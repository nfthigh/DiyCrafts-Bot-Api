import os
import sys
import logging
import asyncio
import hashlib
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv
import requests

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

import psycopg2
from psycopg2.extras import RealDictCursor

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not API_TOKEN:
    raise Exception("TELEGRAM_BOT_TOKEN не определён")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не определена")
MERCHANT_USER_ID = os.getenv("MERCHANT_USER_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
SERVICE_ID = os.getenv("SERVICE_ID")
MERCHANT_ID = os.getenv("MERCHANT_ID")
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_IDS")
if ADMIN_CHAT_IDS:
    ADMIN_CHAT_IDS = [int(x.strip()) for x in ADMIN_CHAT_IDS.split(",") if x.strip()]
else:
    ADMIN_CHAT_IDS = []
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
SELF_URL = os.getenv("SELF_URL")
RETURN_URL = os.getenv("RETURN_URL")

try:
    db_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    db_conn.autocommit = True
    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    logger.info("Подключение к PostgreSQL выполнено успешно (бот).")
except Exception as e:
    logger.error("Ошибка подключения к PostgreSQL (бот): %s", e)
    raise

# Создаем таблицы, если их нет
create_clients_table = """
CREATE TABLE IF NOT EXISTS clients (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    contact TEXT,
    name TEXT
)
"""
create_orders_table = """
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
"""
try:
    db_cursor.execute(create_clients_table)
    db_cursor.execute(create_orders_table)
    logger.info("Таблицы clients и orders созданы или уже существуют (бот).")
except Exception as e:
    logger.error("Ошибка создания таблиц (бот): %s", e)
    raise

try:
    db_cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_amount INTEGER;")
    db_cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS merchant_prepare_id BIGINT;")
    db_cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS merchant_trans_id TEXT;")
    db_conn.commit()
    logger.info("Столбцы payment_amount, merchant_prepare_id и merchant_trans_id проверены/созданы (бот).")
except Exception as e:
    logger.error("Ошибка добавления столбцов: %s", e)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML",
        link_preview_is_disabled=False,
        protect_content=False
    )
)

# FSM для заказа
class OrderForm(StatesGroup):
    contact = State()
    name = State()
    product = State()
    quantity = State()
    text_design = State()
    photo_design = State()
    location = State()
    delivery_comment = State()

# FSM для управления базой (админ)
class DBManagementState(StatesGroup):
    waiting_for_client_id = State()
    waiting_for_order_id = State()

# FSM для ввода суммы оплаты (админ задает цену)
class OrderApproval(StatesGroup):
    waiting_for_payment_sum = State()

def get_main_keyboard(is_admin=False, is_registered=False):
    builder = ReplyKeyboardBuilder()
    builder.button(text='🔄 Начать сначала')
    builder.button(text='📍 Наша локация')
    builder.button(text='📦 Мои заказы')
    if not is_registered:
        builder.button(text='📞 Отправить контакт', request_contact=True)
    if is_admin:
        builder.button(text='🔧 Управление базой данных')
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

location_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text='📍 Отправить локацию', request_location=True)],
        [KeyboardButton(text='❌ Отменить')]
    ],
    resize_keyboard=True
)

def get_product_keyboard():
    products = ["Кружка", "Брелок", "Кепка", "Визитка", "Футболка", "Худи", "Пазл", "Камень", "Стакан"]
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.button(text=product, callback_data=f'product_{product}')
    builder.adjust(2)
    return builder.as_markup()

def generate_auth_header():
    timestamp = str(int(time.time()))
    digest = hashlib.sha1((timestamp + SECRET_KEY).encode('utf-8')).hexdigest()
    return f"{MERCHANT_USER_ID}:{digest}:{timestamp}"

def create_invoice(amount, phone_number, merchant_trans_id):
    url = "https://api.click.uz/v2/merchant/invoice/create"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Auth": generate_auth_header()
    }
    payload = {
        "service_id": SERVICE_ID,
        "amount": amount,
        "phone_number": phone_number,
        "merchant_trans_id": merchant_trans_id
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info("Click API invoice response: %s", response.json())
        return response.json()
    except Exception as e:
        logger.error("Ошибка запроса к Click API: %s", e)
        return {"error_code": -99, "error_note": "Ошибка запроса к Click API"}

async def create_payment_link(user_id: int, amount: int, merchant_trans_id: str) -> str:
    action = "0"
    sign_time = time.strftime("%Y-%m-%d %H:%M:%S")
    signature_string = f"{merchant_trans_id}{SERVICE_ID}{SECRET_KEY}{amount}{action}{sign_time}"
    signature = hashlib.md5(signature_string.encode()).hexdigest()
    payment_url = (
        f"https://my.click.uz/services/pay?"
        f"service_id={SERVICE_ID}&merchant_id={MERCHANT_ID}&amount={amount:.2f}"
        f"&transaction_param={merchant_trans_id}&return_url={RETURN_URL}&signature={signature}"
    )
    return payment_url

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

@router.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT name, contact, username FROM clients WHERE user_id = %s", (user_id,))
    client = cur.fetchone()
    is_admin = user_id in ADMIN_CHAT_IDS
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("Пожалуйста, напишите в личку для регистрации.")
        return
    if client:
        user_name = client.get("name") or "Уважаемый клиент"
        welcome_message = f"👋 Здравствуйте, {user_name}! Добро пожаловать в наш сервис заказов."
        await message.answer(welcome_message, reply_markup=get_main_keyboard(is_admin, True))
        await message.answer("🌟 Выберите товар из ассортимента:", reply_markup=get_product_keyboard())
        await state.set_state(OrderForm.product)
    else:
        welcome_message = "👋 Добро пожаловать! Отправьте, пожалуйста, контакт для регистрации."
        builder = ReplyKeyboardBuilder()
        builder.button(text='📞 Отправить контакт', request_contact=True)
        keyboard = builder.as_markup(resize_keyboard=True)
        await message.answer(welcome_message, reply_markup=keyboard)
        await state.set_state(OrderForm.contact)

@router.message(StateFilter(OrderForm.contact), F.content_type == types.ContentType.CONTACT)
async def register_contact(message: types.Message, state: FSMContext):
    user_contact = message.contact.phone_number
    await state.update_data(contact=user_contact)
    builder = ReplyKeyboardBuilder()
    builder.button(text='❌ Отменить')
    keyboard = builder.as_markup(resize_keyboard=True)
    await message.answer("💬 Введите ваше имя:", reply_markup=keyboard)
    await state.set_state(OrderForm.name)

@router.message(StateFilter(OrderForm.contact))
async def handle_contact_prompt(message: types.Message):
    await message.reply("Отправьте контакт, используя кнопку '📞 Отправить контакт'.")

@router.message(StateFilter(OrderForm.name))
async def register_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.reply("Введите имя.")
        return
    user_name = message.text.strip()
    if not user_name:
        await message.reply("Имя не может быть пустым.")
        return
    user_id = message.from_user.id
    user_username = message.from_user.username or "не указан"
    data = await state.get_data()
    contact = data.get('contact')
    cur = db_conn.cursor()
    cur.execute("""
        INSERT INTO clients (user_id, username, contact, name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, contact = EXCLUDED.contact, name = EXCLUDED.name
    """, (user_id, user_username, contact, user_name))
    db_conn.commit()
    await state.clear()
    is_admin = user_id in ADMIN_CHAT_IDS
    await message.answer(f"🎉 Спасибо за регистрацию, {user_name}!", reply_markup=get_main_keyboard(is_admin, True))
    await message.answer("🌟 Выберите товар из ассортимента:", reply_markup=get_product_keyboard())
    await state.set_state(OrderForm.product)

@router.callback_query(lambda c: c.data and c.data.startswith('product_'), StateFilter(OrderForm.product))
async def process_product_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    product = callback_query.data.split('_', 1)[1]
    await state.update_data(product=product)
    builder = ReplyKeyboardBuilder()
    builder.button(text='❌ Отменить')
    keyboard = builder.as_markup(resize_keyboard=True)
    await callback_query.message.answer(
        f"✨ Вы выбрали: <b>{product}</b>!\n\nВведите количество (шт.):",
        reply_markup=keyboard
    )
    await state.set_state(OrderForm.quantity)

@router.message(StateFilter(OrderForm.quantity))
async def handle_quantity(message: types.Message, state: FSMContext):
    quantity = message.text.strip()
    if not quantity.isdigit() or int(quantity) <= 0:
        await message.reply("Укажите корректное количество.")
        return
    await state.update_data(quantity=int(quantity))
    builder = ReplyKeyboardBuilder()
    builder.button(text='❌ Отменить')
    keyboard = builder.as_markup(resize_keyboard=True)
    await message.reply("Введите креативный текст для дизайна:", reply_markup=keyboard)
    await state.set_state(OrderForm.text_design)

@router.message(StateFilter(OrderForm.text_design))
async def handle_text_design(message: types.Message, state: FSMContext):
    design_text = message.text.strip()
    await state.update_data(design_text=design_text)
    builder = InlineKeyboardBuilder()
    builder.button(text='📸 Пропустить фото', callback_data='skip_photo')
    builder.button(text='❌ Отменить', callback_data='cancel')
    keyboard = builder.as_markup()
    await message.reply("Прикрепите фото для дизайна или нажмите «Пропустить»:", reply_markup=keyboard)
    await state.set_state(OrderForm.photo_design)

@router.callback_query(lambda c: c.data == 'skip_photo', StateFilter(OrderForm.photo_design))
async def skip_photo_design(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.update_data(design_photo=None)
    await callback_query.message.answer("Поделитесь локацией:", reply_markup=location_keyboard)
    await state.set_state(OrderForm.location)

@router.message(StateFilter(OrderForm.photo_design), F.content_type.in_({types.ContentType.PHOTO, types.ContentType.DOCUMENT}))
async def handle_photo_design(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    await state.update_data(design_photo=file_id)
    await message.reply("Поделитесь локацией:", reply_markup=location_keyboard)
    await state.set_state(OrderForm.location)

@router.message(StateFilter(OrderForm.location), F.content_type == types.ContentType.LOCATION)
async def handle_location(message: types.Message, state: FSMContext):
    location = message.location
    await state.update_data(location=location)
    builder = InlineKeyboardBuilder()
    builder.button(text='💬 Пропустить комментарий', callback_data='skip_comment')
    builder.button(text='❌ Отменить', callback_data='cancel')
    keyboard = builder.as_markup()
    await message.reply("Введите комментарий к доставке или нажмите «Пропустить»:", reply_markup=keyboard)
    await state.set_state(OrderForm.delivery_comment)

@router.message(StateFilter(OrderForm.delivery_comment))
async def handle_delivery_comment(message: types.Message, state: FSMContext):
    delivery_comment = message.text.strip()
    await state.update_data(delivery_comment=delivery_comment)
    # После оформления заказа отправляем уведомление администратору для ввода цены
    await send_order_to_admin(message.from_user.id, state)

@router.callback_query(lambda c: c.data == 'skip_comment', StateFilter(OrderForm.delivery_comment))
async def skip_delivery_comment(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.update_data(delivery_comment="Не указан")
    await send_order_to_admin(callback_query.from_user.id, state)

async def send_order_to_admin(user_id, state: FSMContext):
    data = await state.get_data()
    product = data.get('product')
    quantity = data.get('quantity')
    design_text = data.get('design_text')
    design_photo = data.get('design_photo')
    location = data.get('location')
    delivery_comment = data.get('delivery_comment') or "Не указан"

    # Генерируем UUID для merchant_trans_id
    merchant_trans_id = str(uuid.uuid4())

    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        INSERT INTO orders (user_id, merchant_trans_id, product, quantity, design_text, design_photo,
            location_lat, location_lon, order_time, delivery_comment, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (user_id, merchant_trans_id, product, quantity, design_text, design_photo,
          location.latitude, location.longitude, datetime.now().strftime('%Y-%m-%d %H:%M'), delivery_comment, "Ожидание одобрения"))
    db_conn.commit()
    cur.execute("SELECT order_id, merchant_trans_id FROM orders WHERE user_id = %s ORDER BY order_time DESC LIMIT 1", (user_id,))
    order_row = cur.fetchone()
    order_id = order_row["order_id"] if order_row else None
    merchant_trans_id = order_row["merchant_trans_id"] if order_row else merchant_trans_id
    if not order_id:
        await bot.send_message(user_id, "🚫 Ошибка при создании заказа.")
        return

    order_message = (
        f"💎 Новый заказ №{order_id}\n\n"
        f"👤 Клиент: {user_id}\n"
        f"📦 Товар: {product}\n"
        f"🔢 Количество: {quantity} шт.\n"
        f"📝 Дизайн: {design_text}\n"
        f"💬 Комментарий: {delivery_comment}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить заказ", callback_data=f"approve_{order_id}")
    builder.button(text="❌ Отклонить заказ", callback_data=f"reject_{order_id}")
    markup = builder.as_markup()
    for chat_id in ADMIN_CHAT_IDS + ([int(GROUP_CHAT_ID)] if GROUP_CHAT_ID else []):
        try:
            await bot.send_message(chat_id, order_message, reply_markup=markup)
            await bot.send_location(chat_id, latitude=location.latitude, longitude=location.longitude)
            if design_photo:
                await bot.send_document(chat_id, design_photo)
        except Exception as e:
            logger.error(f"Ошибка отправки заказа в чат {chat_id}: {e}")
    await bot.send_message(user_id, "✅ Ваш заказ отправлен на обработку. Ожидайте подтверждения от администрации.",
                           reply_markup=get_main_keyboard(user_id in ADMIN_CHAT_IDS, True))
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("approve_"))
async def approve_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[1])
    admin_id = callback_query.from_user.id
    if admin_id not in ADMIN_CHAT_IDS:
        await callback_query.answer("Нет прав.", show_alert=True)
        return
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("Одобрен", order_id))
    db_conn.commit()
    # После одобрения просим администратора указать цену
    await state.update_data(approval_order_id=order_id)
    await callback_query.message.answer(f"Введите цену для заказа №{order_id} (сум):")
    await state.set_state(OrderApproval.waiting_for_payment_sum)

@router.message(OrderApproval.waiting_for_payment_sum)
async def process_payment_sum(message: types.Message, state: FSMContext):
    text = message.text.strip()
    try:
        payment_sum = float(text)
    except ValueError:
        await message.reply("🚫 Введите корректное число (сумму).")
        return
    data = await state.get_data()
    order_id = data.get("approval_order_id")
    if not order_id:
        await message.reply("Ошибка: номер заказа не найден.")
        await state.clear()
        return
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("UPDATE orders SET status = %s, payment_amount = %s WHERE order_id = %s", ("Одобрен", int(payment_sum), order_id))
    db_conn.commit()
    await message.reply(f"Цена для заказа №{order_id} установлена: {payment_sum} сум.")
    cur.execute("SELECT user_id FROM orders WHERE order_id = %s", (order_id,))
    result = cur.fetchone()
    if result:
        client_id = result["user_id"]
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить заказ", callback_data=f"confirm_order_{order_id}")
        await bot.send_message(client_id,
                               f"Ваш заказ №{order_id} одобрен с ценой {payment_sum} сум.\nНажмите кнопку ниже для оплаты:",
                               reply_markup=builder.as_markup())
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("confirm_order_"))
async def handle_client_confirmation(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    try:
        order_id = int(callback_query.data.split('_')[2])
    except Exception as e:
        await callback_query.message.answer("Ошибка обработки заказа.")
        return
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT payment_amount, merchant_trans_id FROM orders WHERE order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        await callback_query.message.answer("Ошибка: заказ не найден.")
        return
    amount = order.get("payment_amount")
    if not amount:
        await callback_query.message.answer("Ошибка: сумма заказа не установлена.")
        return
    merchant_trans_id = order.get("merchant_trans_id")
    payment_url = await create_payment_link(callback_query.from_user.id, amount, merchant_trans_id)
    if not payment_url:
        await callback_query.message.answer("Ошибка при создании ссылки на оплату.")
        return
    # Формируем inline-клавиатуру с кнопкой "Оплатить"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=payment_url)]])
    await callback_query.message.answer("Нажмите кнопку ниже для оплаты:", reply_markup=keyboard)

@router.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def reject_order(callback_query: types.CallbackQuery):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[1])
    admin_id = callback_query.from_user.id
    if admin_id not in ADMIN_CHAT_IDS:
        await callback_query.answer("Нет прав.", show_alert=True)
        return
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("Отклонено", order_id))
    db_conn.commit()
    cur.execute("SELECT user_id FROM orders WHERE order_id = %s", (order_id,))
    result = cur.fetchone()
    if result:
        await bot.send_message(result["user_id"], f"🚫 Ваш заказ №{order_id} отклонён.")
    await callback_query.answer("Заказ отклонён.", show_alert=True)
    await callback_query.message.edit_text(f"Заказ №{order_id} отклонён.")

@router.message(lambda message: message.text == "📍 Наша локация")
async def send_static_location(message: types.Message):
    await message.answer_location(latitude=41.306584, longitude=69.308076)

@router.message(lambda message: message.text == "📦 Мои заказы")
async def show_my_orders(message: types.Message):
    user_id = message.from_user.id
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT order_id, product, quantity, order_time, status FROM orders WHERE user_id = %s ORDER BY order_time DESC", (user_id,))
    orders_list = cur.fetchall()
    if not orders_list:
        await message.answer("У вас нет заказов.", reply_markup=get_main_keyboard(user_id in ADMIN_CHAT_IDS, True))
        return
    response_lines = []
    for order in orders_list:
        status = order["status"] or "Неизвестный статус"
        order_time = order["order_time"].strftime('%Y-%m-%d %H:%M')
        line = f"№{order['order_id']}: {order['product']} x{order['quantity']} | {status} | {order_time}"
        response_lines.append(line)
    response_text = "📦 Ваши заказы:\n" + "\n".join(response_lines)
    await message.answer(response_text, reply_markup=get_main_keyboard(user_id in ADMIN_CHAT_IDS, True))

@router.message(lambda message: message.text == "🔧 Управление базой данных")
async def db_management_menu(message: types.Message):
    if message.from_user.id not in ADMIN_CHAT_IDS:
        await message.answer("Нет прав для управления БД.")
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="Удалить клиента", callback_data="db_delete_client")
    builder.button(text="Удалить заказ", callback_data="db_delete_order")
    builder.button(text="Очистить заказы", callback_data="db_clear_orders")
    builder.adjust(1)
    await message.answer("Выберите действие:", reply_markup=builder.as_markup())

@router.callback_query(lambda c: c.data == "db_delete_client")
async def db_delete_client(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await callback_query.message.answer("Введите user_id клиента для удаления:")
    await state.set_state(DBManagementState.waiting_for_client_id)

@router.message(DBManagementState.waiting_for_client_id)
async def process_client_deletion(message: types.Message, state: FSMContext):
    user_id_text = message.text.strip()
    if not user_id_text.isdigit():
        await message.answer("User ID должен быть числом.")
        return
    user_id = int(user_id_text)
    cur = db_conn.cursor()
    cur.execute("DELETE FROM clients WHERE user_id = %s", (user_id,))
    db_conn.commit()
    await message.answer(f"Клиент с user_id={user_id} удалён (если существовал).", reply_markup=get_main_keyboard(message.from_user.id in ADMIN_CHAT_IDS, True))
    await state.clear()

@router.callback_query(lambda c: c.data == "db_delete_order")
async def db_delete_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await callback_query.message.answer("Введите order_id заказа для удаления:")
    await state.set_state(DBManagementState.waiting_for_order_id)

@router.message(DBManagementState.waiting_for_order_id)
async def process_order_deletion(message: types.Message, state: FSMContext):
    order_id_text = message.text.strip()
    if not order_id_text.isdigit():
        await message.answer("Order ID должен быть числом.")
        return
    order_id = int(order_id_text)
    cur = db_conn.cursor()
    cur.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
    db_conn.commit()
    await message.answer(f"Заказ с order_id={order_id} удалён (если существовал).", reply_markup=get_main_keyboard(message.from_user.id in ADMIN_CHAT_IDS, True))
    await state.clear()

@router.callback_query(lambda c: c.data == "db_clear_orders")
async def db_clear_orders(callback_query: types.CallbackQuery):
    await callback_query.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="Подтвердить удаление всех заказов", callback_data="db_clear_orders_confirm")
    builder.button(text="Отмена", callback_data="db_clear_orders_cancel")
    await callback_query.message.answer("Вы действительно хотите удалить все заказы?", reply_markup=builder.as_markup())

@router.callback_query(lambda c: c.data == "db_clear_orders_confirm")
async def db_clear_orders_confirm(callback_query: types.CallbackQuery):
    await callback_query.answer()
    cur = db_conn.cursor()
    cur.execute("DELETE FROM orders")
    db_conn.commit()
    await callback_query.message.edit_text("Все заказы удалены.")

@router.callback_query(lambda c: c.data == "db_clear_orders_cancel")
async def db_clear_orders_cancel(callback_query: types.CallbackQuery):
    await callback_query.answer("Действие отменено.")
    await callback_query.message.edit_text("Действие отменено.")

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
