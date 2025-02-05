# payment_api.py
import os
import os.path
import sys
import logging

# Настроим логирование в консоль (stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("payment_api")

# Определяем абсолютный путь до config.py и создаем его из CONFIG_CONTENT, если отсутствует
basedir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(basedir, "config.py")
if not os.path.exists(config_path):
    config_content = os.getenv("CONFIG_CONTENT")
    if config_content:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_content)
        logger.info("config.py создан из переменной окружения CONFIG_CONTENT")
    else:
        raise Exception("Переменная окружения CONFIG_CONTENT не установлена.")

import time
import hashlib
import json
import requests
import threading
import sqlite3
from flask import Flask, request, jsonify
from fiscal import create_fiscal_item  # Функция формирования фискальных данных
import config  # Импорт настроек из config.py

app = Flask(__name__)
app.logger = logger

# Настройки из config.py
MERCHANT_USER_ID = config.MERCHANT_USER_ID
SECRET_KEY = config.SECRET_KEY
SERVICE_ID = config.SERVICE_ID
PHONE_NUMBER = config.PHONE_NUMBER
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
GROUP_CHAT_ID = config.GROUP_CHAT_ID
SELF_URL = config.SELF_URL

# Подключаемся к базе данных (файл clients.db должен сохраняться между запусками)
conn = sqlite3.connect('clients.db', check_same_thread=False)
cursor = conn.cursor()

# Обновлённая схема таблицы orders: добавлено поле unit_price (тийины)
cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    product TEXT,
    quantity INTEGER,
    design_text TEXT,
    design_photo TEXT,
    location_lat REAL,
    location_lon REAL,
    cost_info TEXT,
    status TEXT,
    merchant_trans_id TEXT,
    order_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivery_comment TEXT,
    admin_price REAL,
    unit_price REAL,           -- новое поле для цены за единицу (тийины)
    payment_url TEXT,
    is_paid INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES clients (user_id)
)
""")
conn.commit()
app.logger.info("База данных и таблица orders инициализированы.")

def generate_auth_header():
    timestamp = str(int(time.time()))
    digest = hashlib.sha1((timestamp + SECRET_KEY).encode('utf-8')).hexdigest()
    header = f"{MERCHANT_USER_ID}:{digest}:{timestamp}"
    app.logger.info("Сгенерирован auth header: %s", header)
    return header

def notify_admins(message_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": GROUP_CHAT_ID, "text": message_text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
        app.logger.info("Уведомление отправлено: %s", message_text)
    except Exception as e:
        app.logger.error("Ошибка отправки уведомления в Telegram: %s", e)

@app.route("/click-api/create_invoice", methods=["POST"])
def create_invoice():
    app.logger.info("Получен запрос на создание инвойса: %s", request.data.decode())
    data = request.get_json() or request.form
    app.logger.info("Данные запроса: %s", data)

    required_fields = ["merchant_trans_id", "amount", "phone_number"]
    for field in required_fields:
        if field not in data:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    merchant_trans_id = data["merchant_trans_id"]
    try:
        amount = float(data["amount"])
    except Exception as e:
        error_msg = f"Ошибка преобразования amount: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400
    phone_number = data["phone_number"]

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Auth": generate_auth_header(),
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    payload = {
        "service_id": SERVICE_ID,
        "amount": amount,  # сумма платежа в суммах
        "phone_number": phone_number,
        "merchant_trans_id": merchant_trans_id
    }
    app.logger.info("Payload для создания инвойса: %s", json.dumps(payload, indent=2))
    try:
        resp = requests.post("https://api.click.uz/v2/merchant/invoice/create",
                             headers=headers,
                             json=payload,
                             timeout=30)
        app.logger.info("HTTP-статус создания инвойса: %s", resp.status_code)
        if resp.status_code != 200:
            app.logger.error("Invoice creation failed: %s", resp.text)
            return jsonify({
                "error": "-9",
                "error_note": "Invoice creation failed",
                "http_code": resp.status_code,
                "response": resp.text
            }), 200
        invoice_data = resp.json()
        app.logger.info("Invoice created: %s", json.dumps(invoice_data, indent=2))
        return jsonify(invoice_data), 200
    except Exception as e:
        app.logger.error("Invoice creation exception: %s", str(e))
        return jsonify({"error": "-9", "error_note": str(e)}), 200

@app.route("/click-api/prepare", methods=["POST"])
def prepare():
    app.logger.info("Получен запрос на prepare: %s", request.data.decode())
    required_fields = ["click_trans_id", "merchant_trans_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    app.logger.info("Prepare: click_trans_id=%s, merchant_trans_id=%s", click_trans_id, merchant_trans_id)
    cursor.execute("UPDATE orders SET status=?, cost_info=? WHERE merchant_trans_id=?",
                   ("pending", click_trans_id, merchant_trans_id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO orders (merchant_trans_id, status, cost_info) VALUES (?, ?, ?)",
                       (merchant_trans_id, "pending", click_trans_id))
        app.logger.info("Новый заказ создан в режиме prepare.")
    else:
        app.logger.info("Заказ обновлён в режиме prepare.")
    conn.commit()
    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": merchant_trans_id,
        "error": "0",
        "error_note": "Success"
    }
    app.logger.info("Ответ prepare: %s", json.dumps(response, indent=2))
    return jsonify(response)

@app.route("/click-api/complete", methods=["POST"])
def complete():
    app.logger.info("Получен запрос на complete: %s", request.data.decode())
    required_fields = ["click_trans_id", "merchant_trans_id", "merchant_prepare_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    merchant_prepare_id = request.form["merchant_prepare_id"]
    try:
        amount = float(request.form["amount"])
    except Exception as e:
        error_msg = f"Ошибка преобразования amount: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    # Создаем новое соединение с базой данных для получения актуальных данных
    new_conn = sqlite3.connect('clients.db')
    new_cursor = new_conn.cursor()

    # Извлекаем admin_price и unit_price из базы
    new_cursor.execute("SELECT admin_price, unit_price FROM orders WHERE merchant_trans_id=?", (merchant_trans_id,))
    row = new_cursor.fetchone()
    app.logger.info("Данные заказа для unit_price: %s", row)
    if row and row[0] is not None and row[1] is not None:
        admin_price = float(row[0])
        unit_price = float(row[1])
        app.logger.info("unit_price взят из БД: admin_price=%s, unit_price=%s", admin_price, unit_price)
    else:
        error_msg = "Цена заказа не установлена. Пожалуйста, дождитесь подтверждения администратора."
        app.logger.error(error_msg)
        new_conn.close()
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    # Для фискализации игнорируем значение количества, фиксируем его равным 1
    fiscal_quantity = 1

    # Получаем название товара
    new_cursor.execute("SELECT product FROM orders WHERE merchant_trans_id=?", (merchant_trans_id,))
    row = new_cursor.fetchone()
    if row and row[0]:
        product_name = row[0]
    else:
        product_name = "Неизвестный товар"

    app.logger.info(
        "Параметры /complete: click_trans_id=%s, merchant_trans_id=%s, amount=%s, product_name=%s, fiscal_quantity=%s, unit_price=%s",
        click_trans_id, merchant_trans_id, amount, product_name, fiscal_quantity, unit_price
    )

    new_cursor.execute("SELECT * FROM orders WHERE merchant_trans_id=?", (merchant_trans_id,))
    order_row = new_cursor.fetchone()
    app.logger.info("Содержимое заказа: %s", order_row)
    if not order_row:
        error_msg = "Order not found"
        app.logger.error(error_msg)
        new_conn.close()
        return jsonify({"error": "-5", "error_note": error_msg}), 404
    if order_row[-1] == 1:
        error_msg = "Already paid"
        app.logger.error(error_msg)
        new_conn.close()
        return jsonify({"error": "-4", "error_note": error_msg}), 400

    new_cursor.execute("UPDATE orders SET is_paid=1, status='processing' WHERE merchant_trans_id=?", (merchant_trans_id,))
    new_conn.commit()

    try:
        # Формируем фискальный элемент, где GoodPrice = unit_price и количество фиксировано равным 1
        fiscal_item = create_fiscal_item(product_name, fiscal_quantity, unit_price)
        fiscal_items = [fiscal_item]
        app.logger.info("Фискальные данные сформированы: %s", json.dumps(fiscal_items, indent=2, ensure_ascii=False))
    except Exception as e:
        error_msg = f"Ошибка формирования фискальных данных: {e}"
        app.logger.error(error_msg)
        new_conn.close()
        return jsonify({"error": "-10", "error_note": error_msg}), 400

    fiscal_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Auth": generate_auth_header(),
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    fiscal_payload = {
        "service_id": SERVICE_ID,
        "payment_id": click_trans_id,
        "items": fiscal_items,
        "received_ecash": amount,  # сумма платежа в суммах
        "received_cash": 0,
        "received_card": 0
    }
    app.logger.info("Фискальный payload: %s", json.dumps(fiscal_payload, indent=2, ensure_ascii=False))
    try:
        resp_fiscal = requests.post("https://api.click.uz/v2/merchant/payment/ofd_data/submit_items",
                                      headers=fiscal_headers,
                                      json=fiscal_payload,
                                      timeout=30)
        if resp_fiscal.status_code == 200:
            fiscal_result = resp_fiscal.json()
            app.logger.info("Фискальные данные отправлены, ответ: %s", json.dumps(fiscal_result, indent=2, ensure_ascii=False))
        else:
            fiscal_result = {"error_code": -1, "raw": resp_fiscal.text}
            app.logger.error("Ошибка фискализации, статус %s: %s", resp_fiscal.status_code, resp_fiscal.text)
    except Exception as e:
        fiscal_result = {"error_code": -1, "error_note": str(e)}
        app.logger.error("Исключение при фискализации: %s", e)

    new_cursor.execute("UPDATE orders SET status='completed' WHERE merchant_trans_id=?", (merchant_trans_id,))
    new_conn.commit()
    new_conn.close()

    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_confirm_id": merchant_prepare_id,
        "error": "0",
        "error_note": "Success",
        "fiscal_items": fiscal_items,
        "fiscal_response": fiscal_result
    }
    app.logger.info("Ответ /complete отправлен: %s", json.dumps(response, indent=2, ensure_ascii=False))
    return jsonify(response)

def auto_ping():
    """
    Функция автопинга для поддержания активности инстанса на Render.com.
    Каждые 4 минуты отправляет GET-запрос к SELF_URL.
    """
    while True:
        try:
            app.logger.info("Auto-ping: отправка запроса к %s", SELF_URL)
            requests.get(SELF_URL, timeout=10)
        except Exception as e:
            app.logger.error("Auto-ping error: %s", e)
        time.sleep(240)  # 4 минуты

# Запускаем автопинг в отдельном потоке
ping_thread = threading.Thread(target=auto_ping, daemon=True)
ping_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
