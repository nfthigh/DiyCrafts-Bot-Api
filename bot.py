import os
import os.path
import sys
import logging
import asyncio
import uuid
import requests
import json
from datetime import datetime
from dotenv import load_dotenv

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

# Загружаем переменные окружения из .env
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

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

import config  # Импорт настроек из config.py

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_IDS")
if ADMIN_CHAT_IDS:
    ADMIN_CHAT_IDS = [int(x.strip()) for x in ADMIN_CHAT_IDS.split(",")]
else:
    ADMIN_CHAT_IDS = []
# Добавляем дополнительных администраторов
ADMIN_CHAT_IDS.extend([127767391, 37643916])

GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
if GROUP_CHAT_ID:
    GROUP_CHAT_ID = GROUP_CHAT_ID.strip()
SELF_URL = os.getenv("SELF_URL")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не установлена")
try:
    db_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    db_conn.autocommit = True
    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    logger.info("Подключение к PostgreSQL выполнено успешно (бот).")
except Exception as e:
    logger.error("Ошибка подключения к PostgreSQL (бот): %s", e)
    raise

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
    db_cursor.execute(create_clients_table)
    db_cursor.execute(create_orders_table)
    logger.info("Таблицы clients и orders созданы или уже существуют (бот).")
except Exception as e:
    logger.error("Ошибка создания таблиц (бот): %s", e)
    raise

# FSM для заказа товара
class OrderForm(StatesGroup):
    contact = State()
    name = State()
    product = State()
    quantity = State()
    text_design = State()
    photo_design = State()
    location = State()
    delivery_comment = State()

# FSM для ввода суммы администратором
class AdminPriceState(StatesGroup):
    waiting_for_price = State()

# FSM для управления базой данных (админ)
class DBManagementState(StatesGroup):
    waiting_for_client_id = State()
    waiting_for_order_id = State()

def get_main_keyboard(is_admin=False, is_registered=False):
    """
    Формирует основную клавиатуру.
    """
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

# --- Фискальные данные ---
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

def create_fiscal_item(product_name: str, quantity: int, unit_price: float) -> dict:
    """
    Формирует элемент фискальных данных для платежа.
    """
    product = products_data.get(product_name)
    if not product:
        raise ValueError(f"Товар '{product_name}' не найден")
    price_total = unit_price * quantity
    vat = round((price_total / 1.12) * 0.12)
    fiscal_item = {
        "Name": product_name,
        "SPIC": product["SPIC"],
        "PackageCode": product["PackageCode"],
        "GoodPrice": unit_price,
        "Price": price_total,
        "Amount": quantity,
        "VAT": vat,
        "VATPercent": 12,
        "CommissionInfo": product["CommissionInfo"]
    }
    return fiscal_item

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML",
        link_preview_is_disabled=False,
        protect_content=False
    )
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# --- Обработчики команды /start и регистрации ---
@router.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT name, contact, username FROM clients WHERE user_id = %s", (user_id,))
    client = cur.fetchone()
    is_admin = user_id in ADMIN_CHAT_IDS

    if message.chat.type != ChatType.PRIVATE:
        await message.reply("Пожалуйста, напишите мне в личные сообщения для регистрации.")
        return

    if client:
        user_name = client.get("name") or "Уважаемый клиент"
        welcome_message = f"👋 Здравствуйте, {user_name}! Добро пожаловать в наш премиальный сервис заказов."
        await message.answer(welcome_message, reply_markup=get_main_keyboard(is_admin, True))
        await message.answer("🌟 Пожалуйста, выберите товар из нашего ассортимента:", reply_markup=get_product_keyboard())
        await state.set_state(OrderForm.product)
    else:
        welcome_message = "👋 Добро пожаловать! Для начала, отправьте, пожалуйста, свой контакт для регистрации."
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
    await message.answer("💬 Введите, пожалуйста, ваше имя:", reply_markup=keyboard)
    await state.set_state(OrderForm.name)

@router.message(StateFilter(OrderForm.contact))
async def handle_contact_prompt(message: types.Message):
    await message.reply("Пожалуйста, отправьте свой контакт, используя кнопку '📞 Отправить контакт'.")

@router.message(StateFilter(OrderForm.name))
async def register_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.reply("Пожалуйста, введите ваше имя.")
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
    await message.answer("🌟 Пожалуйста, выберите товар из нашего ассортимента:", reply_markup=get_product_keyboard())
    await state.set_state(OrderForm.product)

# --- Обработка выбора товара и создание заказа ---
@router.callback_query(lambda c: c.data and c.data.startswith('product_'), StateFilter(OrderForm.product))
async def process_product_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    product = callback_query.data.split('_', 1)[1]
    await state.update_data(product=product)
    builder = ReplyKeyboardBuilder()
    builder.button(text='❌ Отменить')
    keyboard = builder.as_markup(resize_keyboard=True)
    await callback_query.message.answer(
        f"✨ Вы выбрали: <b>{product}</b>!\n\nПожалуйста, введите количество (шт.):",
        reply_markup=keyboard
    )
    await state.set_state(OrderForm.quantity)

@router.message(StateFilter(OrderForm.quantity))
async def handle_quantity(message: types.Message, state: FSMContext):
    quantity = message.text.strip()
    if not quantity.isdigit() or int(quantity) <= 0:
        await message.reply("🚫 Пожалуйста, укажите корректное количество.")
        return
    await state.update_data(quantity=int(quantity))
    builder = ReplyKeyboardBuilder()
    builder.button(text='❌ Отменить')
    keyboard = builder.as_markup(resize_keyboard=True)
    await message.reply(
        "🎨 Введите креативный текст для дизайна (например, надпись):",
        reply_markup=keyboard
    )
    await state.set_state(OrderForm.text_design)

@router.message(StateFilter(OrderForm.text_design))
async def handle_text_design(message: types.Message, state: FSMContext):
    design_text = message.text.strip()
    await state.update_data(design_text=design_text)
    builder = InlineKeyboardBuilder()
    builder.button(text='📸 Пропустить фото', callback_data='skip_photo')
    builder.button(text='❌ Отменить', callback_data='cancel')
    keyboard = builder.as_markup()
    await message.reply(
        "📷 Добавьте фото для дизайна или нажмите «Пропустить»:",
        reply_markup=keyboard
    )
    await state.set_state(OrderForm.photo_design)

@router.callback_query(lambda c: c.data == 'skip_photo', StateFilter(OrderForm.photo_design))
async def skip_photo_design(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.update_data(design_photo=None)
    await callback_query.message.answer(
        "📍 Поделитесь, пожалуйста, вашей локацией:",
        reply_markup=location_keyboard
    )
    await state.set_state(OrderForm.location)

@router.message(StateFilter(OrderForm.photo_design), F.content_type.in_({types.ContentType.PHOTO, types.ContentType.DOCUMENT}))
async def handle_photo_design(message: types.Message, state: FSMContext):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    await state.update_data(design_photo=file_id)
    await message.reply(
        "📍 Поделитесь, пожалуйста, вашей локацией:",
        reply_markup=location_keyboard
    )
    await state.set_state(OrderForm.location)

@router.message(StateFilter(OrderForm.location), F.content_type == types.ContentType.LOCATION)
async def handle_location(message: types.Message, state: FSMContext):
    location = message.location
    await state.update_data(location=location)
    builder = InlineKeyboardBuilder()
    builder.button(text='💬 Пропустить комментарий', callback_data='skip_comment')
    builder.button(text='❌ Отменить', callback_data='cancel')
    keyboard = builder.as_markup()
    await message.reply(
        "💬 Введите комментарий к доставке или нажмите «Пропустить»:",
        reply_markup=keyboard
    )
    await state.set_state(OrderForm.delivery_comment)

@router.message(StateFilter(OrderForm.delivery_comment))
async def handle_delivery_comment(message: types.Message, state: FSMContext):
    delivery_comment = message.text.strip()
    await state.update_data(delivery_comment=delivery_comment)
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

    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    db_cursor.execute("SELECT name, contact, username FROM clients WHERE user_id = %s", (user_id,))
    client = db_cursor.fetchone()
    if client:
        user_name = client.get("name") or "Уважаемый клиент"
        user_contact = client.get("contact") or "не указан"
        user_username = client.get("username") or "не указан"
    else:
        user_name = "Уважаемый клиент"
        user_contact = "не указан"
        user_username = "не указан"

    order_time = datetime.now().strftime('%Y-%m-%d %H:%M')
    db_cursor.execute("""
        INSERT INTO orders (user_id, product, quantity, design_text, design_photo,
        location_lat, location_lon, order_time, delivery_comment, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (user_id, product, quantity, design_text, design_photo,
          location.latitude, location.longitude, order_time, delivery_comment, "Ожидание одобрения"))
    db_conn.commit()
    db_cursor.execute("SELECT order_id FROM orders WHERE user_id = %s ORDER BY order_time DESC LIMIT 1", (user_id,))
    order_row = db_cursor.fetchone()
    order_id = order_row["order_id"] if order_row else None
    if not order_id:
        await bot.send_message(user_id, "🚫 Произошла ошибка при создании заказа.")
        return

    order_message = (
        f"💎 Новый заказ №{order_id}\n\n"
        f"👤 Клиент: {user_name} (@{user_username}, {user_contact})\n"
        f"📦 Товар: {product}\n"
        f"🔢 Количество: {quantity} шт.\n"
        f"📝 Дизайн: {design_text}\n"
        f"💬 Комментарий: {delivery_comment}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить заказ", callback_data=f"approve_{order_id}")
    builder.button(text="❌ Отклонить заказ", callback_data=f"reject_{order_id}")
    markup = builder.as_markup()
    for chat_id in ADMIN_CHAT_IDS + ([GROUP_CHAT_ID] if GROUP_CHAT_ID else []):
        try:
            await bot.send_message(chat_id, order_message, reply_markup=markup)
            await bot.send_location(chat_id, latitude=location.latitude, longitude=location.longitude)
            if design_photo:
                await bot.send_document(chat_id, design_photo)
        except Exception as e:
            logger.error(f"Ошибка при отправке заказа в чат {chat_id}: {e}")
    await bot.send_message(
        user_id,
        "✅ Ваш заказ успешно отправлен на обработку. Пожалуйста, ожидайте подтверждения от администрации.",
        reply_markup=get_main_keyboard(user_id in ADMIN_CHAT_IDS, True)
    )
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("approve_"))
async def approve_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[1])
    admin_id = callback_query.from_user.id
    if admin_id not in ADMIN_CHAT_IDS:
        await callback_query.answer("У вас нет прав.", show_alert=True)
        return
    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    db_cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("Ожидание суммы", order_id))
    db_conn.commit()
    await state.update_data(order_id=order_id)
    await callback_query.message.answer(
        f"💰 Пожалуйста, введите итоговую сумму заказа (в суммах) для заказа №{order_id}:",
        reply_markup=get_main_keyboard(is_admin=True, is_registered=True)
    )
    await state.set_state(AdminPriceState.waiting_for_price)

@router.message(AdminPriceState.waiting_for_price)
async def process_admin_price(message: types.Message, state: FSMContext):
    price_text = message.text.strip()
    if not price_text.isdigit():
        await message.reply("🚫 Сумма должна быть числом. Попробуйте ещё раз.")
        return
    admin_price_sum = float(price_text)
    data = await state.get_data()
    order_id = data.get('order_id')
    if not order_id:
        await message.reply("🚫 Ошибка: заказ не найден.")
        await state.clear()
        return
    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    db_cursor.execute("UPDATE orders SET admin_price = %s WHERE order_id = %s", (admin_price_sum, order_id))
    db_conn.commit()
    logger.info(f"Цена {admin_price_sum} сум сохранена для заказа {order_id}.")
    db_cursor.execute("SELECT user_id, product, quantity FROM orders WHERE order_id = %s", (order_id,))
    result = db_cursor.fetchone()
    if not result:
        await message.reply("🚫 Ошибка: заказ не найден.")
        await state.clear()
        return
    client_id = result["user_id"]
    product = result["product"]
    quantity = result["quantity"]
    total_amount_sum = admin_price_sum * quantity
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"client_accept_order_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"client_cancel_order_{order_id}")]
    ])
    await bot.send_message(
        client_id,
        f"💳 Ваш заказ №{order_id} подтвержден!\n"
        f"💰 Итоговая сумма к оплате: <b>{total_amount_sum} сум</b>.\n\n"
        "Нажмите кнопку ниже, чтобы подтвердить заказ:",
        reply_markup=inline_kb
    )
    await message.reply("Сумма заказа успешно отправлена клиенту для подтверждения.")
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("client_accept_order_"))
async def client_accept_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[-1])
    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    db_cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("Ожидание оплаты", order_id))
    db_conn.commit()
    db_cursor.execute("SELECT admin_price, product, quantity, user_id FROM orders WHERE order_id = %s", (order_id,))
    result = db_cursor.fetchone()
    if not result:
        await callback_query.message.answer("🚫 Ошибка: заказ не найден.")
        return
    admin_price_sum = float(result["admin_price"])
    product = result["product"]
    quantity = result["quantity"]
    user_id = result["user_id"]
    total_amount_sum = admin_price_sum * quantity

    merchant_trans_id = str(uuid.uuid4())
    db_cursor.execute("UPDATE orders SET merchant_trans_id = %s WHERE order_id = %s", (merchant_trans_id, order_id))
    db_conn.commit()

    db_cursor.execute("SELECT contact FROM clients WHERE user_id = %s", (user_id,))
    client_data = db_cursor.fetchone()
    client_phone = client_data["contact"] if client_data and client_data.get("contact") else ""

    BASE_URL = f"{SELF_URL}/click-api"
    payload = {
        "merchant_trans_id": merchant_trans_id,
        "amount": total_amount_sum,  # сумма платежа в суммах
        "phone_number": client_phone
    }
    logger.info("Отправляем запрос на создание инвойса с payload: %s", json.dumps(payload, indent=2))
    try:
        response = requests.post(f"{BASE_URL}/create_invoice", json=payload, timeout=30)
        invoice_response = response.json()
        logger.info("Ответ от создания инвойса: %s", json.dumps(invoice_response, indent=2))
        payment_url = invoice_response.get("payment_url")
        if not payment_url and invoice_response.get("invoice_id"):
            invoice_id = invoice_response["invoice_id"]
            payment_url = f"https://api.click.uz/pay/invoice/{invoice_id}"
        if not payment_url:
            await callback_query.message.answer("🚫 Ошибка создания инвойса. Детали: " + json.dumps(invoice_response), parse_mode=None)
            return
        db_cursor.execute("UPDATE orders SET payment_url = %s WHERE order_id = %s", (payment_url, order_id))
        db_conn.commit()
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)]
        ])
        await callback_query.message.edit_text(
            f"💎 Ваш заказ №{order_id} успешно оформлен!\n"
            f"💰 Итоговая сумма к оплате: <b>{total_amount_sum} сум</b>.\n\n"
            "Нажмите кнопку ниже для перехода к оплате:",
            reply_markup=inline_kb
        )
    except Exception as e:
        await callback_query.message.answer(f"🚫 Ошибка при создании инвойса: {e}", parse_mode=None)

@router.callback_query(lambda c: c.data and c.data.startswith("client_cancel_order_"))
async def client_cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[-1])
    db_cursor = db_conn.cursor()
    db_cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("Отменён клиентом", order_id))
    db_conn.commit()
    await callback_query.message.edit_text(f"🚫 Ваш заказ №{order_id} был отменён.")
    
@router.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def reject_order(callback_query: types.CallbackQuery):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[1])
    admin_id = callback_query.from_user.id
    if admin_id not in ADMIN_CHAT_IDS:
        await callback_query.answer("Нет прав.", show_alert=True)
        return
    db_cursor = db_conn.cursor()
    db_cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("Отклонено", order_id))
    db_conn.commit()
    db_cursor.execute("SELECT user_id FROM orders WHERE order_id = %s", (order_id,))
    result = db_cursor.fetchone()
    if result:
        client_id = result["user_id"]
        await bot.send_message(client_id, f"🚫 Ваш заказ №{order_id} был отклонён.")
    await callback_query.answer("Заказ отклонён.", show_alert=True)

# --- Обработчик кнопки "📍 Наша локация" ---
@router.message(lambda message: message.text == "📍 Наша локация")
async def send_static_location(message: types.Message):
    # Отправляем фиксированную локацию
    await message.answer_location(latitude=41.306584, longitude=69.308076)

# --- Обработчик кнопки "📦 Мои заказы" ---
@router.message(lambda message: message.text == "📦 Мои заказы")
async def show_my_orders(message: types.Message):
    user_id = message.from_user.id
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT order_id, product, quantity, order_time, status, is_paid FROM orders WHERE user_id = %s ORDER BY order_time DESC", (user_id,))
    orders = cur.fetchall()
    if not orders:
        await message.answer("У вас нет заказов.", reply_markup=get_main_keyboard(message.from_user.id in ADMIN_CHAT_IDS, True))
        return
    response_lines = []
    for order in orders:
        # Если заказ не оплачен, статус может быть "Ожидание оплаты" или иное – выводим как "Заказан (не оплачен)"
        status = order["status"]
        if order.get("is_paid") == 1:
            status_text = "Оплачен"
        else:
            status_text = status if status else "Заказан (не оплачен)"
        order_time = order["order_time"].strftime('%Y-%m-%d %H:%M')
        line = f"№{order['order_id']}: {order['product']} x{order['quantity']} | {status_text} | {order_time}"
        response_lines.append(line)
    response_text = "📦 Ваши заказы:\n" + "\n".join(response_lines)
    await message.answer(response_text, reply_markup=get_main_keyboard(message.from_user.id in ADMIN_CHAT_IDS, True))

# --- Обработчик кнопки "🔧 Управление базой данных" (только для админов) ---
@router.message(lambda message: message.text == "🔧 Управление базой данных")
async def db_management_menu(message: types.Message):
    if message.from_user.id not in ADMIN_CHAT_IDS:
        await message.answer("У вас нет прав для управления базой данных.")
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="Удалить клиента", callback_data="db_delete_client")
    builder.button(text="Удалить заказ", callback_data="db_delete_order")
    builder.button(text="Очистить заказы", callback_data="db_clear_orders")
    builder.adjust(1)
    await message.answer("Выберите действие:", reply_markup=builder.as_markup())

# --- Обработка действий из меню управления БД ---
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
    await message.answer(f"Клиент с user_id={user_id} удалён (если он существовал).", reply_markup=get_main_keyboard(message.from_user.id in ADMIN_CHAT_IDS, True))
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
    await message.answer(f"Заказ с order_id={order_id} удалён (если он существовал).", reply_markup=get_main_keyboard(message.from_user.id in ADMIN_CHAT_IDS, True))
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
