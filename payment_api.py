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
from flask import Flask, request, jsonify, render_template_string
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

# Подключаемся к PostgreSQL
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
    required_fields = ["click_trans_id", "merchant_trans_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    app.logger.info("Prepare: click_trans_id=%s, merchant_trans_id=%s", click_trans_id, merchant_trans_id)
    cursor.execute("UPDATE orders SET status=%s, cost_info=%s WHERE merchant_trans_id=%s", 
                   ("pending", click_trans_id, merchant_trans_id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO orders (merchant_trans_id, status, cost_info) VALUES (%s, %s, %s)",
                       (merchant_trans_id, "pending", click_trans_id))
        app.logger.info("Новый заказ создан в режиме prepare.")
    else:
        app.logger.info("Заказ обновлён в режиме prepare.")
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

    cursor.execute("SELECT admin_price FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    row = cursor.fetchone()
    app.logger.info("Данные заказа для unit_price: %s", row)
    if row and row.get("admin_price"):
        admin_price = float(row["admin_price"])
        unit_price = admin_price * 100  # переводим в тийины
        app.logger.info("unit_price из БД: admin_price=%s, unit_price=%s", admin_price, unit_price)
    else:
        error_msg = "Missing field: unit_price and не удалось извлечь из БД"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    quantity_str = request.form.get("quantity")
    if quantity_str:
        try:
            quantity = int(quantity_str)
        except Exception as e:
            error_msg = f"Ошибка преобразования quantity: {e}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400
    else:
        cursor.execute("SELECT quantity FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
        row = cursor.fetchone()
        if row and row.get("quantity"):
            quantity = int(row["quantity"])
            app.logger.info("Количество (quantity) из БД: %s", quantity)
        else:
            error_msg = "Missing field: quantity and не удалось извлечь из БД"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    cursor.execute("SELECT product FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    row = cursor.fetchone()
    if row and row.get("product"):
        product_name = row["product"]
    else:
        product_name = "Неизвестный товар"

    app.logger.info(
        "Параметры /complete: click_trans_id=%s, merchant_trans_id=%s, amount=%s, product_name=%s, quantity=%s, unit_price=%s",
        click_trans_id, merchant_trans_id, amount, product_name, quantity, unit_price
    )

    cursor.execute("SELECT * FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    order_row = cursor.fetchone()
    app.logger.info("Содержимое заказа: %s", order_row)
    if not order_row:
        error_msg = "Order not found"
        app.logger.error(error_msg)
        return jsonify({"error": "-5", "error_note": error_msg}), 404
    if order_row.get("is_paid") == 1:
        error_msg = "Already paid"
        app.logger.error(error_msg)
        return jsonify({"error": "-4", "error_note": error_msg}), 400

    cursor.execute("UPDATE orders SET is_paid=1, status='processing' WHERE merchant_trans_id=%s", (merchant_trans_id,))
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

# Новый endpoint для автоматического сабмита формы Payme
@app.route("/auto_payme", methods=["GET"])
def auto_payme_redirect():
    order_id = request.args.get("order_id", "")
    amount = request.args.get("amount", "")
    merchant = request.args.get("merchant", "")
    callback_url = request.args.get("callback", "")
    lang = request.args.get("lang", "ru")
    description = f"Оплата заказа №{order_id}"
    
    html_template = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
      <meta charset="UTF-8">
      <title>Перенаправление на оплату</title>
      <script>
        window.onload = function() {
          document.getElementById('payme_form').submit();
        };
      </script>
      <style>
        body {
          font-family: Arial, sans-serif;
          text-align: center;
          margin-top: 50px;
          color: #333;
        }
      </style>
    </head>
    <body>
      <h2>Пожалуйста, подождите...</h2>
      <p>Мы автоматически перенаправляем вас на страницу оплаты. Это может занять несколько секунд 😊🙏</p>
      <form action="https://checkout.paycom.uz" method="POST" id="payme_form">
        <input type="hidden" name="account[order_id]" value="{{ order_id }}">
        <input type="hidden" name="amount" value="{{ amount }}">
        <input type="hidden" name="merchant" value="{{ merchant }}">
        <input type="hidden" name="callback" value="{{ callback_url }}">
        <input type="hidden" name="lang" value="{{ lang }}">
        <input type="hidden" name="description" value="{{ description }}">
        <noscript>
          <input type="submit" value="Оплатить">
        </noscript>
      </form>
    </body>
    </html>
    """
    return render_template_string(html_template,
                                  order_id=order_id,
                                  amount=amount,
                                  merchant=merchant,
                                  callback_url=callback_url,
                                  lang=lang,
                                  description=description)

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
