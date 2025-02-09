# payment_api.py
import os
import sys
import logging
import time
import hashlib
import json
import requests
import threading
import psycopg2
from flask import Flask, request, jsonify
import config

# Настроим логирование в консоль (stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("payment_api")

app = Flask(__name__)
app.logger = logger

# Функция для подключения к базе PostgreSQL
def get_db_connection():
    return psycopg2.connect(config.DATABASE_URL)

# Функция генерации заголовка аутентификации
def generate_auth_header():
    timestamp = str(int(time.time()))
    digest = hashlib.sha1((timestamp + config.SECRET_KEY).encode('utf-8')).hexdigest()
    header = f"{config.MERCHANT_USER_ID}:{digest}:{timestamp}"
    app.logger.info("Сгенерирован auth header: %s", header)
    return header

# Функция уведомления администраторов через Telegram
def notify_admins(message_text):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.GROUP_CHAT_ID, "text": message_text, "parse_mode": "HTML"}
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
        "service_id": config.SERVICE_ID,
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
    
    conn_db = get_db_connection()
    cursor_db = conn_db.cursor()
    # Обновляем запись, если она существует, или создаём новую
    cursor_db.execute("UPDATE orders SET status=%s, cost_info=%s WHERE merchant_trans_id=%s",
                      ("pending", click_trans_id, merchant_trans_id))
    if cursor_db.rowcount == 0:
        cursor_db.execute("INSERT INTO orders (merchant_trans_id, status, cost_info) VALUES (%s, %s, %s)",
                          (merchant_trans_id, "pending", click_trans_id))
        app.logger.info("Новый заказ создан в режиме prepare.")
    else:
        app.logger.info("Заказ обновлён в режиме prepare.")
    conn_db.commit()
    cursor_db.close()
    conn_db.close()
    
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

    # Подключаемся к базе PostgreSQL
    conn_db = get_db_connection()
    cursor_db = conn_db.cursor()
    cursor_db.execute("SELECT admin_price, unit_price, product FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    row = cursor_db.fetchone()
    if row and row[0] is not None and row[1] is not None:
        admin_price = float(row[0])
        unit_price = float(row[1])
        product_name = row[2] if row[2] else "Неизвестный товар"
        app.logger.info("Получены admin_price=%s, unit_price=%s, product_name=%s", admin_price, unit_price, product_name)
    else:
        cursor_db.close()
        conn_db.close()
        return jsonify({"error": "-8", "error_note": "Цена заказа не установлена. Пожалуйста, дождитесь подтверждения администратора."}), 400

    # Для фискализации фиксируем количество равным 1
    fiscal_quantity = 1

    # Продолжаем логику: проверяем, что заказ существует и ещё не оплачен
    cursor_db.execute("SELECT * FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    order_row = cursor_db.fetchone()
    if not order_row:
        cursor_db.close()
        conn_db.close()
        return jsonify({"error": "-5", "error_note": "Order not found"}), 404
    # Предположим, что поле is_paid находится в конце строки заказа
    if order_row[-1] == True:
        cursor_db.close()
        conn_db.close()
        return jsonify({"error": "-4", "error_note": "Already paid"}), 400

    cursor_db.execute("UPDATE orders SET is_paid=%s, status=%s WHERE merchant_trans_id=%s",
                      (True, "processing", merchant_trans_id))
    conn_db.commit()

    # Пример формирования фискального элемента (здесь ваша логика)
    # Для демонстрации просто формируем словарь с данными
    fiscal_item = {
        "Name": product_name,
        "GoodPrice": unit_price,  # unit_price в тийинах
        "Price": unit_price * fiscal_quantity,
        "Amount": fiscal_quantity,
        "VAT": round((unit_price * fiscal_quantity / 1.12) * 0.12),
        "VATPercent": 12,
        "CommissionInfo": {"TIN": "307022362"}
    }
    fiscal_items = [fiscal_item]
    app.logger.info("Фискальные данные сформированы: %s", json.dumps(fiscal_items, indent=2, ensure_ascii=False))

    # Отправка фискальных данных (пример запроса к внешнему API)
    fiscal_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Auth": generate_auth_header(),
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    fiscal_payload = {
        "service_id": config.SERVICE_ID,
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

    cursor_db.execute("UPDATE orders SET status=%s WHERE merchant_trans_id=%s", ("completed", merchant_trans_id))
    conn_db.commit()
    cursor_db.close()
    conn_db.close()

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
    Функция автопинга для поддержания активности приложения.
    Каждые 4 минуты отправляет GET-запрос к SELF_URL.
    """
    while True:
        try:
            app.logger.info("Auto-ping: отправка запроса к %s", config.SELF_URL)
            requests.get(config.SELF_URL, timeout=10)
        except Exception as e:
            app.logger.error("Auto-ping error: %s", e)
        time.sleep(240)

# Запускаем автопинг в отдельном потоке
ping_thread = threading.Thread(target=auto_ping, daemon=True)
ping_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
