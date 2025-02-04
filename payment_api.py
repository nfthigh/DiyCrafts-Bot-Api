# payment_api.py
import os

# Если файла config.py нет, создаём его из переменной окружения CONFIG_CONTENT
if not os.path.exists("config.py"):
    config_content = os.getenv("CONFIG_CONTENT")
    if config_content:
        with open("config.py", "w") as f:
            f.write(config_content)
    else:
        raise Exception("Переменная окружения CONFIG_CONTENT не установлена.")

import time
import uuid
import hashlib
import json
import requests
import threading
import sqlite3
from flask import Flask, request, jsonify
from fiscal import create_fiscal_item
import config  # Импорт настроек

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
        print("Ошибка отправки уведомления в Telegram:", e)

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
            app.logger.error(f"Invoice creation failed: {resp.text}")
            return jsonify({
                "error": "-9",
                "error_note": "Invoice creation failed",
                "http_code": resp.status_code,
                "response": resp.text
            }), 200
        app.logger.info("Invoice created: " + json.dumps(resp.json()))
        return jsonify(resp.json()), 200
    except Exception as e:
        app.logger.error("Invoice creation exception: " + str(e))
        return jsonify({"error": "-9", "error_note": str(e)}), 200

@app.route("/click-api/prepare", methods=["POST"])
def prepare():
    required_fields = ["click_trans_id", "merchant_trans_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            return jsonify({"error": "-8", "error_note": f"Missing field: {field}"}), 400
    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    amount = float(request.form["amount"])
    # Используем поле cost_info вместо несуществующего total
    cursor.execute("UPDATE orders SET cost_info=?, status=? WHERE merchant_trans_id=?",
                   (str(amount), "pending", merchant_trans_id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO orders (merchant_trans_id, cost_info, status) VALUES (?, ?, ?)",
                       (merchant_trans_id, str(amount), "pending"))
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
            return jsonify({"error": "-8", "error_note": f"Missing field: {field}"}), 400
    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    merchant_prepare_id = request.form["merchant_prepare_id"]
    amount = float(request.form["amount"])
    product_name = request.form["product_name"]
    quantity = int(request.form["quantity"])
    unit_price = float(request.form["unit_price"])
    cursor.execute("SELECT * FROM orders WHERE merchant_trans_id=?", (merchant_trans_id,))
    order_row = cursor.fetchone()
    if not order_row:
        return jsonify({"error": "-5", "error_note": "Order not found"}), 404
    if order_row[-1] == 1:
        return jsonify({"error": "-4", "error_note": "Already paid"}), 400
    cursor.execute("UPDATE orders SET is_paid=1, status='processing' WHERE merchant_trans_id=?", (merchant_trans_id,))
    conn.commit()
    try:
        fiscal_item = create_fiscal_item(product_name, quantity, unit_price)
        fiscal_items = [fiscal_item]
    except Exception as e:
        return jsonify({"error": "-10", "error_note": str(e)}), 400
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
        else:
            fiscal_result = {"error_code": -1, "raw": resp_fiscal.text}
    except Exception as e:
        fiscal_result = {"error_code": -1, "error_note": str(e)}
    notification_message = (
        "💰 <b>Оплата прошла успешно!</b> 💰\n\n"
        f"✅ Заказ <b>{merchant_trans_id}</b> оплачен.\n"
        f"📦 Товар: <b>{product_name}</b>\n"
        f"🔢 Количество: <b>{quantity}</b>\n"
        f"💸 Цена за единицу: <b>{unit_price/100}</b> сум (преобразовано в {unit_price} тийинов)\n"
        f"🧾 Итоговая сумма: <b>{amount}</b> тийинов\n\n"
        "📄 Фискальные данные:\n"
        f"<pre>{json.dumps(fiscal_items, indent=2, ensure_ascii=False)}</pre>"
    )
    notify_admins(notification_message)
    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_confirm_id": merchant_prepare_id,
        "error": "0",
        "error_note": "Success",
        "fiscal_items": fiscal_items,
        "fiscal_response": fiscal_result
    }
    return jsonify(response)

def autopinger():
    while True:
        time.sleep(300)
        if SELF_URL:
            try:
                print("[AUTO-PING] Пингуем:", SELF_URL)
                requests.get(SELF_URL, timeout=10)
            except Exception as e:
                print("[AUTO-PING] Ping error:", e)
        else:
            print("[AUTO-PING] SELF_URL not set. Waiting...")

def run_autopinger_thread():
    thread = threading.Thread(target=autopinger, daemon=True)
    thread.start()

if __name__ == "__main__":
    run_autopinger_thread()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
