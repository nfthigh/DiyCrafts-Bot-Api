import os
import sys
import logging
import time
import hashlib
import json
import requests
import threading
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from fiscal import create_fiscal_item  # Функция формирования фискальных данных

# Загружаем переменные окружения из .env
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("payment_api")

# Получаем настройки из переменных окружения
MERCHANT_USER_ID = os.getenv("MERCHANT_USER_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
SERVICE_ID = int(os.getenv("SERVICE_ID"))
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
SELF_URL = os.getenv("SELF_URL")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не установлена")
try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = True
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    logger.info("Подключение к PostgreSQL выполнено успешно.")
except Exception as e:
    logger.error("Ошибка подключения к PostgreSQL: %s", e)
    raise

# Создаем таблицу orders, если не существует
create_table_query = """
CREATE TABLE IF NOT EXISTS orders (
    order_id SERIAL PRIMARY KEY,
    user_id BIGINT,
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
    payment_url TEXT,
    is_paid INTEGER DEFAULT 0
)
"""
try:
    cursor.execute(create_table_query)
    logger.info("Таблица orders создана или уже существует.")
except Exception as e:
    logger.error("Ошибка создания таблицы orders: %s", e)
    raise

app = Flask(__name__)
app.logger = logger

def generate_auth_header():
    timestamp = str(int(time.time()))
    digest = hashlib.sha1((timestamp + SECRET_KEY).encode('utf-8')).hexdigest()
    header = f"{MERCHANT_USER_ID}:{digest}:{timestamp}"
    app.logger.info("Сгенерирован auth header: %s", header)
    return header

def md5_hash(s):
    return hashlib.md5(s.encode('utf-8')).hexdigest()

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
        "amount": amount,
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
    # Обязательные параметры согласно документации Prepare
    required_fields = [
        "click_trans_id", "service_id", "click_paydoc_id",
        "merchant_trans_id", "amount", "action", "sign_time", "sign_string"
    ]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    service_id = request.form["service_id"]
    click_paydoc_id = request.form["click_paydoc_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    try:
        amount = float(request.form["amount"])
    except Exception as e:
        error_msg = f"Ошибка преобразования amount: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400
    action = request.form["action"]
    sign_time = request.form["sign_time"]
    sign_string = request.form["sign_string"]

    # Для Prepare action должно быть равно "0"
    if action != "0":
        error_msg = "Неверное значение параметра action для Prepare (ожидается 0)"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    # Вычисляем подпись согласно MD5( click_trans_id + service_id + SECRET_KEY + merchant_trans_id + amount + action + sign_time )
    data_str = f"{click_trans_id}{service_id}{SECRET_KEY}{merchant_trans_id}{amount}{action}{sign_time}"
    expected_sign = md5_hash(data_str)
    if expected_sign != sign_string:
        error_msg = "Неверная подпись (sign_string) в запросе Prepare"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    # Резервируем заказ: если заказ с данным merchant_trans_id уже существует – обновляем статус,
    # иначе создаём новый заказ и получаем order_id как merchant_prepare_id
    cursor.execute("UPDATE orders SET status=%s, cost_info=%s WHERE merchant_trans_id=%s", 
                   ("pending", click_trans_id, merchant_trans_id))
    if cursor.rowcount == 0:
        cursor.execute(
            "INSERT INTO orders (merchant_trans_id, status, cost_info) VALUES (%s, %s, %s) RETURNING order_id",
            (merchant_trans_id, "pending", click_trans_id)
        )
        new_order = cursor.fetchone()
        if new_order and new_order.get("order_id"):
            merchant_prepare_id = new_order["order_id"]
        else:
            merchant_prepare_id = merchant_trans_id
        app.logger.info("Новый заказ создан в режиме prepare, order_id: %s", merchant_prepare_id)
    else:
        cursor.execute("SELECT order_id FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
        row = cursor.fetchone()
        if row and row.get("order_id"):
            merchant_prepare_id = row["order_id"]
        else:
            merchant_prepare_id = merchant_trans_id
        app.logger.info("Заказ обновлён в режиме prepare, order_id: %s", merchant_prepare_id)

    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": merchant_prepare_id,
        "error": "0",
        "error_note": "Success"
    }
    app.logger.info("Ответ prepare: %s", json.dumps(response, indent=2))
    return jsonify(response)

@app.route("/click-api/complete", methods=["POST"])
def complete():
    app.logger.info("Получен запрос на complete: %s", request.data.decode())
    # Обязательные параметры согласно документации Complete
    required_fields = [
        "click_trans_id", "service_id", "click_paydoc_id",
        "merchant_trans_id", "merchant_prepare_id", "amount",
        "action", "sign_time", "sign_string"
    ]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    service_id = request.form["service_id"]
    click_paydoc_id = request.form["click_paydoc_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    merchant_prepare_id = request.form["merchant_prepare_id"]
    try:
        amount = float(request.form["amount"])
    except Exception as e:
        error_msg = f"Ошибка преобразования amount: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400
    action = request.form["action"]
    sign_time = request.form["sign_time"]
    sign_string = request.form["sign_string"]

    # Для Complete action должно быть равно "1"
    if action != "1":
        error_msg = "Неверное значение параметра action для Complete (ожидается 1)"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    # Вычисляем подпись согласно MD5( click_trans_id + service_id + SECRET_KEY + merchant_trans_id + merchant_prepare_id + amount + action + sign_time )
    data_str = f"{click_trans_id}{service_id}{SECRET_KEY}{merchant_trans_id}{merchant_prepare_id}{amount}{action}{sign_time}"
    expected_sign = md5_hash(data_str)
    if expected_sign != sign_string:
        error_msg = "Неверная подпись (sign_string) в запросе Complete"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    # Дополнительные параметры от CLICK (error и error_note) можно получить, если передаются
    error_param = request.form.get("error", "0")
    error_note_param = request.form.get("error_note", "")

    cursor.execute("SELECT * FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    order_row = cursor.fetchone()
    if not order_row:
        error_msg = "Order not found"
        app.logger.error(error_msg)
        return jsonify({"error": "-5", "error_note": error_msg}), 404
    if order_row.get("is_paid") == 1:
        error_msg = "Already paid"
        app.logger.error(error_msg)
        return jsonify({"error": "-4", "error_note": error_msg}), 400

    # Отмечаем заказ как оплаченный и переводим его в статус 'processing'
    cursor.execute("UPDATE orders SET is_paid=1, status='processing' WHERE merchant_trans_id=%s", (merchant_trans_id,))
    conn.commit()

    # Извлекаем данные для формирования фискальных данных
    cursor.execute("SELECT admin_price, product, quantity FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    row = cursor.fetchone()
    if row and row.get("admin_price"):
        try:
            admin_price = float(row["admin_price"])
        except Exception as e:
            error_msg = f"Ошибка преобразования admin_price: {e}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400
        unit_price = admin_price * 100  # перевод в тийины
    else:
        error_msg = "Отсутствует admin_price в заказе"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    quantity = row.get("quantity", 1)
    product_name = row.get("product", "Неизвестный товар")
    app.logger.info(
        "Параметры /complete: click_trans_id=%s, merchant_trans_id=%s, amount=%s, product_name=%s, quantity=%s, unit_price=%s",
        click_trans_id, merchant_trans_id, amount, product_name, quantity, unit_price
    )

    try:
        fiscal_item = create_fiscal_item(product_name, quantity, unit_price)
        fiscal_items = [fiscal_item]
        app.logger.info("Фискальные данные сформированы: %s", json.dumps(fiscal_items, indent=2, ensure_ascii=False))
    except Exception as e:
        error_msg = f"Ошибка формирования фискальных данных: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-10", "error_note": error_msg}), 400

    # Отправляем фискальные данные
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
        "received_ecash": amount,
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

    cursor.execute("UPDATE orders SET status='completed' WHERE merchant_trans_id=%s", (merchant_trans_id,))
    conn.commit()

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
        time.sleep(240)

ping_thread = threading.Thread(target=auto_ping, daemon=True)
ping_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
