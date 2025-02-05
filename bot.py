# bot.py
import os
import sys
import logging
import asyncio
import sqlite3
import uuid
import requests
import json
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# Настроим логирование в консоль (stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

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

import config  # Импорт настроек

# Параметры из config
API_TOKEN = config.TELEGRAM_BOT_TOKEN
ADMIN_CHAT_IDS = config.ADMIN_CHAT_IDS
GROUP_CHAT_ID = config.GROUP_CHAT_ID
SELF_URL = config.SELF_URL  # URL для обращения к серверу

# --- Функция формирования фискальных данных (интегрированная версия fiscal.py) ---
# Здесь хранится информация о товарах; можно вынести в отдельный файл products.py или оставить здесь.
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
    
    :param product_name: Название товара (например, "Кружка")
    :param quantity: Количество товара
    :param unit_price: Цена за единицу (в тийинах)
    :return: Словарь с фискальными данными
    """
    product = products_data.get(product_name)
    if not product:
        raise ValueError(f"Товар '{product_name}' не найден")
    price_total = unit_price * quantity
    # Вычисляем НДС: предполагается, что ставка 12%
    vat = round((price_total / 1.12) * 0.12)
    fiscal_item = {
        "Name": product_name,
        "SPIC": product["SPIC"],
        "PackageCode": product["PackageCode"],
        "GoodPrice": unit_price,  # Значение из базы (тийины)
        "Price": price_total,
        "Amount": quantity,
        "VAT": vat,
        "VATPercent": 12,
        "CommissionInfo": product["CommissionInfo"]
    }
    return fiscal_item

# --- Конец блока fiscal ---

# Инициализация бота
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

# Подключаемся к базе данных (один файл для бота и сервера, например, clients.db)
conn = sqlite3.connect('clients.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS clients (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    contact TEXT,
    name TEXT
)
''')
# Обновлённая схема таблицы orders: добавлено поле unit_price
cursor.execute('''
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
    unit_price REAL,          -- новое поле для цены за единицу (тийины)
    payment_url TEXT,
    is_paid INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES clients (user_id)
)
''')
conn.commit()
logger.info("База данных и таблицы инициализированы.")

# --- FSM состояния для оформления заказа ---
class OrderForm(StatesGroup):
    contact = State()
    name = State()
    product = State()
    quantity = State()
    text_design = State()
    photo_design = State()
    location = State()
    delivery_comment = State()

class AdminPriceState(StatesGroup):
    waiting_for_price = State()

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

# --- Обработчики бота ---

@router.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    cursor.execute("SELECT name, contact, username FROM clients WHERE user_id=?", (user_id,))
    client = cursor.fetchone()
    is_admin = user_id in ADMIN_CHAT_IDS

    if message.chat.type != ChatType.PRIVATE:
        await message.reply("Пожалуйста, напишите мне в личные сообщения для регистрации.")
        return

    if client:
        user_name = client[0] if client[0] else "Неизвестный"
        welcome_message = f"👋 Добро пожаловать, {user_name}! Выберите опцию:"
        await message.answer(welcome_message, reply_markup=get_main_keyboard(is_admin, True))
        await message.answer("Выберите продукт:", reply_markup=get_product_keyboard())
        await state.set_state(OrderForm.product)
    else:
        welcome_message = "👋 Добро пожаловать! Отправьте, пожалуйста, свой контакт для регистрации."
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
    await message.answer("Введите ваше имя:", reply_markup=keyboard)
    await state.set_state(OrderForm.name)

@router.message(StateFilter(OrderForm.contact))
async def handle_contact_prompt(message: types.Message):
    await message.reply("Пожалуйста, отправьте свой контакт, воспользовавшись кнопкой '📞 Отправить контакт'.")

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
    user_username = message.from_user.username or "Не указан"
    data = await state.get_data()
    contact = data.get('contact')
    cursor.execute("""
        INSERT INTO clients (user_id, username, contact, name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, contact=excluded.contact, name=excluded.name
    """, (user_id, user_username, contact, user_name))
    conn.commit()
    await state.clear()
    is_admin = user_id in ADMIN_CHAT_IDS
    await message.answer(f"🎉 Спасибо за регистрацию, {user_name}!", reply_markup=get_main_keyboard(is_admin, True))
    await message.answer("Выберите продукт:", reply_markup=get_product_keyboard())
    await state.set_state(OrderForm.product)

@router.callback_query(lambda c: c.data and c.data.startswith('product_'), StateFilter(OrderForm.product))
async def process_product_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    product = callback_query.data.split('_', 1)[1]
    await state.update_data(product=product)
    builder = ReplyKeyboardBuilder()
    builder.button(text='❌ Отменить')
    keyboard = builder.as_markup(resize_keyboard=True)
    await callback_query.message.answer(f"Вы выбрали: {product}. Укажите количество:", reply_markup=keyboard)
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
    await message.reply("Введите надпись для дизайна:", reply_markup=keyboard)
    await state.set_state(OrderForm.text_design)

@router.message(StateFilter(OrderForm.text_design))
async def handle_text_design(message: types.Message, state: FSMContext):
    design_text = message.text.strip()
    await state.update_data(design_text=design_text)
    builder = InlineKeyboardBuilder()
    builder.button(text='Пропустить фото', callback_data='skip_photo')
    builder.button(text='❌ Отменить', callback_data='cancel')
    keyboard = builder.as_markup()
    await message.reply("Добавьте фото для дизайна или пропустите этот шаг:", reply_markup=keyboard)
    await state.set_state(OrderForm.photo_design)

@router.callback_query(lambda c: c.data == 'skip_photo', StateFilter(OrderForm.photo_design))
async def skip_photo_design(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.update_data(design_photo=None)
    await callback_query.message.answer("Отправьте локацию:", reply_markup=location_keyboard)
    await state.set_state(OrderForm.location)

@router.message(StateFilter(OrderForm.photo_design), F.content_type.in_({types.ContentType.PHOTO, types.ContentType.DOCUMENT}))
async def handle_photo_design(message: types.Message, state: FSMContext):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    await state.update_data(design_photo=file_id)
    await message.reply("Отправьте локацию:", reply_markup=location_keyboard)
    await state.set_state(OrderForm.location)

@router.message(StateFilter(OrderForm.location), F.content_type == types.ContentType.LOCATION)
async def handle_location(message: types.Message, state: FSMContext):
    location = message.location
    await state.update_data(location=location)
    builder = InlineKeyboardBuilder()
    builder.button(text='Пропустить комментарий', callback_data='skip_comment')
    builder.button(text='❌ Отменить', callback_data='cancel')
    keyboard = builder.as_markup()
    await message.reply("Введите комментарий к доставке или пропустите:", reply_markup=keyboard)
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

    cursor.execute("SELECT name, contact, username FROM clients WHERE user_id=?", (user_id,))
    client = cursor.fetchone()
    if client:
        user_name = client[0] if client[0] else "Неизвестный"
        user_contact = client[1] if client[1] else "Не указан"
        user_username = client[2] if client[2] else "Не указан"
    else:
        user_name = "Неизвестный"
        user_contact = "Не указан"
        user_username = "Не указан"

    order_time = datetime.now().strftime('%Y-%m-%d %H:%M')
    cursor.execute("""
        INSERT INTO orders (user_id, product, quantity, design_text, design_photo,
        location_lat, location_lon, order_time, delivery_comment, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Ожидание одобрения')
    """, (
        user_id, product, quantity, design_text, design_photo,
        location.latitude, location.longitude, order_time, delivery_comment
    ))
    conn.commit()
    order_id = cursor.lastrowid
    order_message = (
        f"📣 <b>Новый заказ #{order_id}</b> 📣\n\n"
        f"👤 <b>Заказчик:</b> {user_name} (@{user_username}, {user_contact})\n"
        f"📦 <b>Продукт:</b> {product}\n"
        f"🔢 <b>Количество:</b> {quantity}\n"
        f"📝 <b>Дизайн:</b> {design_text}\n"
        f"🗒️ <b>Комментарий:</b> {delivery_comment}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_{order_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{order_id}")
    markup = builder.as_markup()
    recipients = ADMIN_CHAT_IDS + [GROUP_CHAT_ID]
    for chat_id in recipients:
        try:
            await bot.send_message(chat_id, order_message, reply_markup=markup)
            await bot.send_location(chat_id, latitude=location.latitude, longitude=location.longitude)
            if design_photo:
                await bot.send_document(chat_id, design_photo)
        except Exception as e:
            logger.error(f"Error sending order to chat {chat_id}: {e}")
    await bot.send_message(
        user_id,
        "Ваш заказ отправлен. Ожидайте подтверждения.",
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
    cursor.execute("UPDATE orders SET status='Ожидание цены' WHERE order_id=?", (order_id,))
    conn.commit()
    await state.update_data(order_id=order_id)
    await callback_query.message.answer(f"Введите цену за единицу для заказа #{order_id} (в суммах):")
    await state.set_state(AdminPriceState.waiting_for_price)

@router.message(AdminPriceState.waiting_for_price)
async def process_admin_price(message: types.Message, state: FSMContext):
    price_text = message.text.strip()
    if not price_text.isdigit():
        await message.reply("Цена должна быть числом.")
        return
    admin_price_sum = float(price_text)
    # Вычисляем unit_price в тийинах
    unit_price = admin_price_sum * 100
    data = await state.get_data()
    order_id = data.get('order_id')
    if not order_id:
        await message.reply("Ошибка: заказ не найден.")
        await state.clear()
        return
    # Обновляем сразу два поля: admin_price (суммы) и unit_price (тийины)
    cursor.execute("UPDATE orders SET admin_price=?, unit_price=? WHERE order_id=?", (admin_price_sum, unit_price, order_id))
    conn.commit()
    logger.info(f"Цена {admin_price_sum} сум и unit_price {unit_price} тийинов сохранены для заказа {order_id}.")
    cursor.execute("SELECT user_id, product, quantity FROM orders WHERE order_id=?", (order_id,))
    result = cursor.fetchone()
    if not result:
        await message.reply("Ошибка: заказ не найден.")
        await state.clear()
        return
    client_id, product, quantity = result
    total_amount_sum = admin_price_sum * quantity
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен", callback_data=f"client_accept_order_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"client_cancel_order_{order_id}")]
    ])
    await bot.send_message(
        client_id,
        f"Ваш заказ #{order_id} одобрен!\nЦена за единицу: {admin_price_sum} сум (GoodPrice = {unit_price} тийинов).\n"
        f"Итоговая сумма: {total_amount_sum} сум.\nПодтверждаете заказ?",
        reply_markup=inline_kb
    )
    await message.reply("Цена отправлена клиенту на подтверждение.")
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("client_accept_order_"))
async def client_accept_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[-1])
    cursor.execute("UPDATE orders SET status='Ожидание оплаты' WHERE order_id=?", (order_id,))
    conn.commit()
    cursor.execute("SELECT admin_price, product, quantity, user_id FROM orders WHERE order_id=?", (order_id,))
    result = cursor.fetchone()
    if not result:
        await callback_query.message.answer("Ошибка: заказ не найден.")
        return
    admin_price_sum, product, quantity, user_id = result
    unit_price = admin_price_sum * 100
    total_amount_sum = admin_price_sum * quantity

    # Генерируем уникальный merchant_trans_id (UUID)
    merchant_trans_id = str(uuid.uuid4())
    cursor.execute("UPDATE orders SET merchant_trans_id=? WHERE order_id=?", (merchant_trans_id, order_id))
    conn.commit()

    cursor.execute("SELECT contact FROM clients WHERE user_id=?", (user_id,))
    client_data = cursor.fetchone()
    client_phone = client_data[0] if client_data and client_data[0] else ""

    BASE_URL = f"{config.SELF_URL}/click-api"
    payload = {
        "merchant_trans_id": merchant_trans_id,
        "amount": total_amount_sum,  # Сумма в суммах
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
            await callback_query.message.answer("Ошибка создания инвойса. Детали: " + json.dumps(invoice_response), parse_mode=None)
            return
        cursor.execute("UPDATE orders SET payment_url=? WHERE order_id=?", (payment_url, order_id))
        conn.commit()
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)]
        ])
        await callback_query.message.edit_text(
            f"Заказ #{order_id} подтвержден.\nЦена за единицу: {admin_price_sum} сум (GoodPrice = {unit_price} тийинов).\n"
            f"Итоговая сумма: {total_amount_sum} сум.\nНажмите кнопку ниже для оплаты.",
            reply_markup=inline_kb
        )
    except Exception as e:
        await callback_query.message.answer(f"Ошибка при создании инвойса: {e}", parse_mode=None)

@router.callback_query(lambda c: c.data and c.data.startswith("client_cancel_order_"))
async def client_cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[-1])
    cursor.execute("UPDATE orders SET status='Отменён клиентом' WHERE order_id=?", (order_id,))
    conn.commit()
    await callback_query.message.edit_text(f"Заказ #{order_id} отменён клиентом.")

@router.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def reject_order(callback_query: types.CallbackQuery):
    await callback_query.answer()
    order_id = int(callback_query.data.split('_')[1])
    admin_id = callback_query.from_user.id
    if admin_id not in ADMIN_CHAT_IDS:
        await callback_query.answer("Нет прав.", show_alert=True)
        return
    cursor.execute("UPDATE orders SET status='Отклонено' WHERE order_id=?", (order_id,))
    conn.commit()
    cursor.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,))
    result = cursor.fetchone()
    if result:
        client_id = result[0]
        await bot.send_message(client_id, f"Ваш заказ #{order_id} отклонён.")
    await callback_query.answer("Заказ отклонён.", show_alert=True)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
