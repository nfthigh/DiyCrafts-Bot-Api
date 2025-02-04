# payment_api.py
import os
# Определяем абсолютный путь до каталога текущего файла
basedir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(basedir, "config.py")

if not os.path.exists(config_path):
    config_content = os.getenv("CONFIG_CONTENT")
    if config_content:
        with open(config_path, "w") as f:
            f.write(config_content)
    else:
        raise Exception("Переменная окружения CONFIG_CONTENT не установлена.")
import time
import hashlib
import json
import requests
import threading
import sqlite3
from flask import Flask, request, jsonify
from fiscal import create_fiscal_item
import config  # Импорт настроек из config.py

app = Flask(__name__)

MERCHANT_USER_ID = config.MERCHANT_USER_ID
SECRET_KEY = config.SECRET_KEY
SERVICE_ID = config.SERVICE_ID
PHONE_NUMBER = config.PHONE_NUMBER
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
GROUP_CHAT_ID = config.GROUP_CHAT_ID
SELF_URL = config.SELF_URL

# Подключаемся к базе данных
conn = sqlite3.connect('clients.db', check_same_thread=False)
cursor = conn.cursor()

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
    payment_url TEXT,
    is_paid INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES clients (user_id)
)
""")
conn.commit()

def generate_auth_header():
    timestamp = str(int(time.time()))
    digest = hashlib.sha1((timestamp + SECRET_KEY).encode('utf-8')).hexdigest()
    return f"{MERCHANT_USER_ID}:{digest}:{timestamp}"

def notify_admins(message_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": GROUP_CHAT_ID, "text": message_text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        app.logger.error("Ошибка отправки уведомления в Telegram: %s", e)

@app.route("/click-api/create_invoice", methods=["POST"])
def create_invoice():
    # Сначала пытаемся получить данные из JSON, иначе из form
    data = request.get_json() or request.form

    required_fields = ["merchant_trans_id", "amount", "phone_number"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": "-8", "error_note": f"Missing field: {field}"}), 400

    merchant_trans_id = data["merchant_trans_id"]
    amount = float(data["amount"])
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
    try:
        resp = requests.post("https://api.click.uz/v2/merchant/invoice/create",
                             headers=headers,
                             json=payload,
                             timeout=30)
        if resp.status_code != 200:
            app.logger.error("Invoice creation failed: %s", resp.text)
            return jsonify({
                "error": "-9",
                "error_note": "Invoice creation failed",
                "http_code": resp.status_code,
                "response": resp.text
            }), 200
        app.logger.info("Invoice created: %s", json.dumps(resp.json()))
        return jsonify(resp.json()), 200
    except Exception as e:
        app.logger.error("Invoice creation exception: %s", str(e))
        return jsonify({"error": "-9", "error_note": str(e)}), 200

@app.route("/click-api/prepare", methods=["POST"])
def prepare():
    required_fields = ["click_trans_id", "merchant_trans_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            return jsonify({"error": "-8", "error_note": f"Missing field: {field}"}), 400
    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    # Обновляем запись без использования несуществующей колонки total
    cursor.execute("UPDATE orders SET status=?, cost_info=? WHERE merchant_trans_id=?",
                   ("pending", click_trans_id, merchant_trans_id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO orders (merchant_trans_id, status, cost_info) VALUES (?, ?, ?)",
                       (merchant_trans_id, "pending", click_trans_id))
    conn.commit()
    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": merchant_trans_id,
        "error": "0",
        "error_note": "Success"
    }
    return jsonify(response)

@app.route("/click-api/complete", methods=["POST"])
def complete():
    required_fields = ["click_trans_id", "merchant_trans_id", "merchant_prepare_id", "amount", "product_name", "quantity", "unit_price"]
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
    product_name = request.form["product_name"]
    try:
        quantity = int(request.form["quantity"])
    except Exception as e:
        error_msg = f"Ошибка преобразования quantity: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400
    try:
        unit_price = float(request.form["unit_price"])
    except Exception as e:
        error_msg = f"Ошибка преобразования unit_price: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    app.logger.info("Параметры /complete: click_trans_id=%s, merchant_trans_id=%s, amount=%s, product_name=%s, quantity=%s, unit_price=%s",
                      click_trans_id, merchant_trans_id, amount, product_name, quantity, unit_price)

    cursor.execute("SELECT * FROM orders WHERE merchant_trans_id=?", (merchant_trans_id,))
    order_row = cursor.fetchone()
    if not order_row:
        error_msg = "Order not found"
        app.logger.error(error_msg)
        return jsonify({"error": "-5", "error_note": error_msg}), 404
    if order_row[-1] == 1:
        error_msg = "Already paid"
        app.logger.error(error_msg)
        return jsonify({"error": "-4", "error_note": error_msg}), 400

    cursor.execute("UPDATE orders SET is_paid=1, status='processing' WHERE merchant_trans_id=?", (merchant_trans_id,))
    conn.commit()

    try:
        fiscal_item = create_fiscal_item(product_name, quantity, unit_price)
        fiscal_items = [fiscal_item]
        app.logger.info("Фискальные данные сформированы: %s", json.dumps(fiscal_items, indent=2, ensure_ascii=False))
    except Exception as e:
        error_msg = f"Ошибка формирования фискальных данных: {e}"
        app.logger.error(error_msg)
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
        "received_ecash": amount,
        "received_cash": 0,
        "received_card": 0
    }
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

    # Обновляем статус заказа на "completed"
    cursor.execute("UPDATE orders SET status='completed' WHERE merchant_trans_id=?", (merchant_trans_id,))
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
        # Пауза 4 минуты (240 секунд)
        time.sleep(240)

# Запускаем автопинг в отдельном потоке
ping_thread = threading.Thread(target=auto_ping, daemon=True)
ping_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
