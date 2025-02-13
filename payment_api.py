import os
import hashlib
import time
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
import logging
import sys
import requests  # Для отправки запросов к Telegram API
import threading  # Для автопинга

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования (stdout – логи выводятся, например, в Render)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

MERCHANT_USER_ID = os.getenv("MERCHANT_USER_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
SERVICE_ID = os.getenv("SERVICE_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не установлена")

# Читаем токен бота и chat_id группы (если требуется)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # Если группа не используется, можно оставить пустым

# Глобальная переменная подключения к БД
db_conn = None

def connect_db():
    global db_conn
    try:
        db_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        db_conn.autocommit = True
        logger.info("Успешное подключение к БД.")
    except Exception as e:
        logger.error("Ошибка подключения к БД: %s", e)
        raise

connect_db()

def get_db_cursor():
    global db_conn
    try:
        cursor = db_conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT 1")
        return cursor
    except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
        logger.error("Ошибка соединения с БД, переподключаемся: %s", e)
        try:
            db_conn.close()
        except Exception as ex:
            logger.error("Ошибка закрытия соединения: %s", ex)
        connect_db()
        return db_conn.cursor(cursor_factory=RealDictCursor)

def init_db():
    cursor = get_db_cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id SERIAL PRIMARY KEY,
                user_id BIGINT,
                merchant_trans_id TEXT,
                product TEXT,
                quantity INTEGER,
                design_text TEXT,
                design_photo TEXT,
                location_lat REAL,
                location_lon REAL,
                status TEXT,
                payment_amount INTEGER,
                order_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivery_comment TEXT
            )
        """)
        # Дополнительные столбцы для Click
        cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS merchant_prepare_id BIGINT;")
        cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS merchant_trans_id TEXT;")
        db_conn.commit()
        logger.info("Схема БД и таблица orders инициализированы.")
    except Exception as e:
        logger.error("Ошибка инициализации БД: %s", e)
        db_conn.rollback()

init_db()

def calculate_md5(*args):
    concat_str = ''.join(str(arg) for arg in args)
    md5_hash = hashlib.md5(concat_str.encode('utf-8')).hexdigest()
    logger.info("Вычисленная MD5 подпись для %s: %s", concat_str, md5_hash)
    return md5_hash

def build_fiscal_item(order):
    product = order.get("product")
    quantity = order.get("quantity")
    total_price = order.get("payment_amount")
    if not total_price or not quantity:
        raise ValueError("Некорректные данные заказа для фискализации.")
    unit_price = round(total_price / quantity)
    vat = round((total_price / 1.12) * 0.12)
    products_data = {
        "Кружка": {
            "SPIC": "06912001036000000",
            "PackageCode": "1184747",
            "CommissionInfo": {"TIN": "307022362"}
        },
        "Брелок": {
            "SPIC": "07117001015000000",
            "PackageCode": "1156259",
            "CommissionInfo": {"TIN": "307022362"}
        }
    }
    product_info = products_data.get(product)
    if not product_info:
        raise ValueError(f"Нет данных для товара '{product}'.")
    fiscal = {
        "Name": f"{product} (шт)",
        "SPIC": product_info["SPIC"],
        "Units": 1,
        "PackageCode": product_info["PackageCode"],
        "GoodPrice": unit_price,
        "Price": total_price,
        "Amount": quantity,
        "VAT": vat,
        "VATPercent": 12,
        "CommissionInfo": product_info["CommissionInfo"]
    }
    logger.info("Фискальные данные сформированы: %s", fiscal)
    return fiscal

def extract_order_by_mti(merchant_trans_id):
    cursor = get_db_cursor()
    cursor.execute("SELECT * FROM orders WHERE merchant_trans_id = %s", (merchant_trans_id,))
    order = cursor.fetchone()
    logger.info("Извлечён заказ для merchant_trans_id=%s: %s", merchant_trans_id, order)
    return order

def get_request_data():
    try:
        if request.content_type and request.content_type.startswith("application/json"):
            data = request.get_json(force=True)
        elif request.content_type and request.content_type.startswith("application/x-www-form-urlencoded"):
            data = request.form.to_dict()
        else:
            data = {}
        if not data:
            data = request.args.to_dict()
        logger.info("Полученные данные запроса: %s", data)
        return data
    except Exception as e:
        logger.error("Ошибка получения данных: %s", e)
        return {}

def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не установлен")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, data=payload)
        logger.info("Отправлено сообщение в Telegram (chat_id=%s): %s", chat_id, response.text)
    except Exception as e:
        logger.error("Ошибка отправки сообщения в Telegram: %s", e)

@app.route('/click/prepare', methods=['POST'])
def click_prepare():
    logger.info("Запрос PREPARE получен")
    logger.info("Headers: %s", request.headers)
    logger.info("Body: %s", request.data)
    data = get_request_data()
    if not data:
        logger.error("Нет данных в запросе")
        return jsonify({'error': -8, 'error_note': 'Отсутствуют данные'}), 400
    required_fields = ['click_trans_id', 'service_id', 'merchant_trans_id', 'amount', 'action', 'sign_time', 'sign_string']
    if not all(field in data for field in required_fields):
        logger.error("PREPARE: Отсутствуют обязательные параметры. Данные: %s", data)
        return jsonify({'error': -8, 'error_note': 'Отсутствуют обязательные параметры'}), 400
    calc_sign = calculate_md5(
        data['click_trans_id'],
        data['service_id'],
        SECRET_KEY,
        data['merchant_trans_id'],
        data['amount'],
        data['action'],
        data['sign_time']
    )
    if calc_sign != data['sign_string']:
        logger.error("PREPARE: SIGN CHECK FAILED! Вычисленная: %s, полученная: %s", calc_sign, data['sign_string'])
        return jsonify({'error': -1, 'error_note': 'SIGN CHECK FAILED!'}), 400
    order = extract_order_by_mti(data['merchant_trans_id'])
    if not order:
        logger.error("PREPARE: Заказ не найден для merchant_trans_id=%s", data['merchant_trans_id'])
        return jsonify({'error': -5, 'error_note': 'Заказ не найден'}), 200
    merchant_prepare_id = int(time.time())
    cursor = get_db_cursor()
    cursor.execute("UPDATE orders SET merchant_prepare_id = %s WHERE merchant_trans_id = %s", (merchant_prepare_id, data['merchant_trans_id']))
    db_conn.commit()
    logger.info("PREPARE: Обновлён заказ merchant_trans_id=%s, merchant_prepare_id=%s", data['merchant_trans_id'], merchant_prepare_id)
    response = {
        'click_trans_id': data['click_trans_id'],
        'merchant_trans_id': data['merchant_trans_id'],
        'merchant_prepare_id': merchant_prepare_id,
        'error': 0,
        'error_note': 'Success'
    }
    logger.info("PREPARE: Ответ: %s", response)
    return jsonify(response), 200

@app.route('/click/complete', methods=['POST'])
def click_complete():
    logger.info("Запрос COMPLETE получен")
    logger.info("Headers: %s", request.headers)
    logger.info("Body: %s", request.data)
    data = get_request_data()
    if not data:
        logger.error("Нет данных в запросе")
        return jsonify({'error': -8, 'error_note': 'Отсутствуют данные'}), 400
    required_fields = ['click_trans_id', 'service_id', 'merchant_trans_id', 'merchant_prepare_id', 'amount', 'action', 'sign_time', 'sign_string']
    if not all(field in data for field in required_fields):
        logger.error("COMPLETE: Отсутствуют обязательные параметры. Данные: %s", data)
        return jsonify({'error': -8, 'error_note': 'Отсутствуют обязательные параметры'}), 400
    calc_sign = calculate_md5(
        data['click_trans_id'],
        data['service_id'],
        SECRET_KEY,
        data['merchant_trans_id'],
        data['merchant_prepare_id'],
        data['amount'],
        data['action'],
        data['sign_time']
    )
    if calc_sign != data['sign_string']:
        logger.error("COMPLETE: SIGN CHECK FAILED! Вычисленная: %s, полученная: %s", calc_sign, data['sign_string'])
        return jsonify({'error': -1, 'error_note': 'SIGN CHECK FAILED!'}), 400
    order = extract_order_by_mti(data['merchant_trans_id'])
    try:
        db_prepare = int(order.get("merchant_prepare_id"))
        req_prepare = int(data['merchant_prepare_id'])
    except Exception as e:
        logger.error("Ошибка преобразования merchant_prepare_id: %s", e)
        return jsonify({'error': -2, 'error_note': 'Invalid merchant_prepare_id format'}), 400
    if not order or db_prepare != req_prepare:
        logger.error("COMPLETE: Заказ не найден или merchant_prepare_id не совпадает для merchant_trans_id=%s", data['merchant_trans_id'])
        return jsonify({'error': -6, 'error_note': 'Transaction does not exist'}), 200

    # Обновляем статус заказа на "paid"
    cursor = get_db_cursor()
    cursor.execute("UPDATE orders SET status = %s WHERE merchant_trans_id = %s", ("paid", data['merchant_trans_id']))
    db_conn.commit()
    logger.info("COMPLETE: Статус заказа обновлён на paid для merchant_trans_id=%s", data['merchant_trans_id'])

    # --- Отправка уведомлений в Telegram ---
    cursor = get_db_cursor()
    cursor.execute("SELECT * FROM orders WHERE merchant_trans_id = %s", (data['merchant_trans_id'],))
    order = cursor.fetchone()

    if order:
        cursor.execute("SELECT * FROM clients WHERE user_id = %s", (order["user_id"],))
        client = cursor.fetchone()

        client_name = client.get("name", "Неизвестный") if client else "Неизвестный"
        client_username = client.get("username", "") if client else ""
        client_contact = client.get("contact", "Не указан") if client else "Не указан"
        username_display = f" (@{client_username})" if client_username else ""

        message_text = (
            f"✅ Оплата заказа №{order['order_id']} успешно проведена!\n\n"
            f"Клиент: {client_name}{username_display}\n"
            f"Телефон: {client_contact}\n\n"
            f"Товар: {order['product']}\n"
            f"Количество: {order['quantity']} шт.\n"
            f"Сумма: {order['payment_amount']} сум\n"
            f"Комментарий к доставке: {order['delivery_comment']}"
        )

        if GROUP_CHAT_ID:
            send_telegram_message(GROUP_CHAT_ID, message_text)
        send_telegram_message(order["user_id"], message_text)
        logger.info("COMPLETE: Уведомления отправлены: %s", message_text)
    else:
        logger.error("COMPLETE: Не удалось получить данные заказа для уведомлений.")
    # --- /Отправка уведомлений в Telegram ---

    try:
        fiscal_item = build_fiscal_item(order)
    except Exception as e:
        logger.error("COMPLETE: Ошибка формирования фискальных данных: %s", e)
        fiscal_item = {}
    merchant_confirm_id = int(time.time())
    response = {
        'click_trans_id': data['click_trans_id'],
        'merchant_trans_id': data['merchant_trans_id'],
        'merchant_confirm_id': merchant_confirm_id,
        'fiscal_items': fiscal_item,
        'error': 0,
        'error_note': 'Success'
    }
    logger.info("COMPLETE: Ответ: %s", response)
    return jsonify(response), 200

# Функция автопинга для Render.com
def auto_ping():
    auto_ping_url = os.getenv("AUTO_PING_URL")
    if not auto_ping_url:
        logger.warning("AUTO_PING_URL не задан. Автопинг не запущен.")
        return
    while True:
        try:
            response = requests.get(auto_ping_url)
            logger.info("Автопинг: запрос к %s выполнен успешно. Код ответа: %s", auto_ping_url, response.status_code)
        except Exception as e:
            logger.error("Ошибка автопинга: %s", e)
        time.sleep(300)  # каждые 5 минут

# Запускаем автопинг в фоновом потоке
threading.Thread(target=auto_ping, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
