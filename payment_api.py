import os
import hashlib
import time
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
import logging
import sys

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования (stdout – логи видны на Render)
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

# Подключаемся к PostgreSQL
try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = True
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    logger.info("Подключение к PostgreSQL выполнено успешно (payment_api).")
except Exception as e:
    logger.error("Ошибка подключения к БД (payment_api): %s", e)
    raise

# Обновляем схему: создаем таблицу orders, если её нет, и добавляем столбец merchant_prepare_id
try:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id SERIAL PRIMARY KEY,
            user_id BIGINT,
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
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS merchant_prepare_id BIGINT;")
    conn.commit()
    logger.info("Схема базы данных обновлена (orders).")
except Exception as e:
    logger.error("Ошибка обновления схемы базы данных: %s", e)

# Каталог товаров для формирования фискальных данных (пример)
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
    },
    "Кепка": {
        "SPIC": "06506001022000000",
        "PackageCode": "1324746",
        "CommissionInfo": {"TIN": "307022362"}
    },
    "Визитка": {
        "SPIC": "04911001003000000",
        "PackageCode": "1156221",
        "CommissionInfo": {"TIN": "307022362"}
    },
    "Футболка": {
        "SPIC": "06109001001000000",
        "PackageCode": "1124331",
        "CommissionInfo": {"TIN": "307022362"}
    },
    "Худи": {
        "SPIC": "06212001012000000",
        "PackageCode": "1238867",
        "CommissionInfo": {"TIN": "307022362"}
    },
    "Пазл": {
        "SPIC": "04811001019000000",
        "PackageCode": "1748791",
        "CommissionInfo": {"TIN": "307022362"}
    },
    "Камень": {
        "SPIC": "04911001017000000",
        "PackageCode": "1156234",
        "CommissionInfo": {"TIN": "307022362"}
    },
    "Стакан": {
        "SPIC": "07013001008000000",
        "PackageCode": "1345854",
        "CommissionInfo": {"TIN": "307022362"}
    }
}

def calculate_md5(*args):
    concat_str = ''.join(str(arg) for arg in args)
    return hashlib.md5(concat_str.encode('utf-8')).hexdigest()

def build_fiscal_item(order):
    product = order.get("product")
    quantity = order.get("quantity")
    total_price = order.get("payment_amount")
    if not total_price or not quantity:
        raise ValueError("Некорректные данные заказа для фискализации.")
    unit_price = round(total_price / quantity)
    vat = round((total_price / 1.12) * 0.12)
    product_info = products_data.get(product)
    if not product_info:
        raise ValueError(f"Нет данных для товара '{product}'.")
    return {
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

def extract_order_id(merchant_trans_id):
    if not merchant_trans_id.startswith("order_"):
        return None
    try:
        return int(merchant_trans_id.split("_")[1])
    except Exception:
        return None

@app.route('/click/prepare', methods=['POST'])
def click_prepare():
    data = request.get_json()
    logger.info("Получен запрос PREPARE: %s", data)
    required_fields = ['click_trans_id', 'service_id', 'merchant_trans_id', 'amount', 'action', 'sign_time', 'sign_string']
    if not all(field in data for field in required_fields):
        logger.error("PREPARE: Отсутствуют обязательные параметры")
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
        logger.error("PREPARE: SIGN CHECK FAILED!")
        return jsonify({'error': -1, 'error_note': 'SIGN CHECK FAILED!'}), 400

    order_id = extract_order_id(data['merchant_trans_id'])
    if not order_id:
        logger.error("PREPARE: Некорректный merchant_trans_id")
        return jsonify({'error': -5, 'error_note': 'Некорректный merchant_trans_id'}), 200

    cursor.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cursor.fetchone()
    if not order:
        logger.error("PREPARE: Заказ не найден для order_id=%s", order_id)
        return jsonify({'error': -5, 'error_note': 'Заказ не найден'}), 200

    merchant_prepare_id = int(time.time())
    cursor.execute("UPDATE orders SET merchant_prepare_id = %s WHERE order_id = %s", (merchant_prepare_id, order_id))
    conn.commit()
    logger.info("PREPARE: Обновлён заказ %s с merchant_prepare_id=%s", order_id, merchant_prepare_id)

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
    data = request.get_json()
    logger.info("Получен запрос COMPLETE: %s", data)
    required_fields = ['click_trans_id', 'service_id', 'merchant_trans_id', 'merchant_prepare_id', 'amount', 'action', 'sign_time', 'sign_string']
    if not all(field in data for field in required_fields):
        logger.error("COMPLETE: Отсутствуют обязательные параметры")
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
        logger.error("COMPLETE: SIGN CHECK FAILED!")
        return jsonify({'error': -1, 'error_note': 'SIGN CHECK FAILED!'}), 400

    order_id = extract_order_id(data['merchant_trans_id'])
    if not order_id:
        logger.error("COMPLETE: Некорректный merchant_trans_id")
        return jsonify({'error': -5, 'error_note': 'Некорректный merchant_trans_id'}), 200

    cursor.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cursor.fetchone()
    if not order or order.get("merchant_prepare_id") != data['merchant_prepare_id']:
        logger.error("COMPLETE: Заказ не найден или merchant_prepare_id не совпадает для order_id=%s", order_id)
        return jsonify({'error': -6, 'error_note': 'Transaction does not exist'}), 200

    cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("paid", order_id))
    conn.commit()
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
