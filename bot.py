# bot.py
import os
import logging
import asyncio
import sqlite3
import uuid
import requests
import threading
import time
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

# Если файла config.py нет, создаём его из переменной окружения CONFIG_CONTENT
if not os.path.exists("config.py"):
    config_content = os.getenv("CONFIG_CONTENT")
    if config_content:
        with open("config.py", "w") as f:
            f.write(config_content)
    else:
        raise Exception("Переменная окружения CONFIG_CONTENT не установлена.")

import config  # Импорт настроек

API_TOKEN = config.TELEGRAM_BOT_TOKEN
ADMIN_CHAT_IDS = config.ADMIN_CHAT_IDS
GROUP_CHAT_ID = config.GROUP_CHAT_ID
SELF_URL = config.SELF_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Подключаемся к базе данных (тот же файл, что и для payment_api)
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
    payment_url TEXT,
    is_paid INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES clients (user_id)
)
''')
conn.commit()

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
    await message.reply("Пожалуйста, отправьте свой контакт.")

@router.message(StateFilter(OrderForm.name))
async def register_name(message: types.Message, state: FSMContext):
    user_name = message.text.strip()
    if not user_name:
        await message.reply("Имя не может быть пустым.")
        return
    await state.update_data(name=user_name)
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
    await bot.send_message(user_id, "Ваш заказ отправлен. Ожидайте подтверждения.", reply_markup=get_main_keyboard(user_id in ADMIN_CHAT_IDS, True))
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
    # Цена вводится в суммах; для создания инвойса передаем сумму как есть,
    # а для фискализации цена переводится в тийины (1 сум = 100 тийинов)
    admin_price_sum = float(price_text)
    admin_price_tiyin = admin_price_sum * 100
    data = await state.get_data()
    order_id = data.get('order_id')
    if not order_id:
        await message.reply("Ошибка: заказ не найден.")
        await state.clear()
        return
    cursor.execute("UPDATE orders SET admin_price=? WHERE order_id=?", (admin_price_sum, order_id))
    conn.commit()
    cursor.execute("SELECT user_id, product, quantity FROM orders WHERE order_id=?", (order_id,))
    result = cursor.fetchone()
    if not result:
        await message.reply("Ошибка: заказ не найден.")
        await state.clear()
        return
    client_id, product, quantity = result
    total_amount_sum = admin_price_sum * quantity  # итоговая сумма в суммах (без умножения)
    total_amount = total_amount_sum  # для создания инвойса передаем сумму в суммах
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен", callback_data=f"client_accept_order_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"client_cancel_order_{order_id}")]
    ])
    await bot.send_message(client_id, 
        f"Ваш заказ #{order_id} одобрен!\nЦена за единицу: {admin_price_sum} сум (преобразовано в {admin_price_tiyin} тийинов).\n"
        f"Итоговая сумма: {total_amount_sum} сум (для инвойса: {total_amount} сум).\nПодтверждаете заказ?",
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
    unit_price_tiyin = admin_price_sum * 100
    total_amount = unit_price_tiyin * quantity
    total_amount_sum = admin_price_sum * quantity
    import uuid
    merchant_trans_id = f"order_{order_id}_{uuid.uuid4().hex[:6]}"
    cursor.execute("UPDATE orders SET merchant_trans_id=? WHERE order_id=?", (merchant_trans_id, order_id))
    conn.commit()
    cursor.execute("SELECT contact FROM clients WHERE user_id=?", (user_id,))
    client_data = cursor.fetchone()
    client_phone = client_data[0] if client_data and client_data[0] else ""
    BASE_URL = f"{config.SELF_URL}/click-api"
    payload = {
        "merchant_trans_id": merchant_trans_id,
        "amount": total_amount_sum,  # передаем сумму в суммах
        "phone_number": client_phone
    }
    try:
        response = requests.post(f"{BASE_URL}/create_invoice", json=payload, timeout=30)
        invoice_response = response.json()
        print("Invoice response:", invoice_response)  # Логируем полный ответ
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
            f"Заказ #{order_id} подтвержден.\nЦена за единицу: {admin_price_sum} сум (преобразовано в {unit_price_tiyin} тийинов).\n"
            f"Итоговая сумма: {total_amount_sum} сум ({total_amount} тийинов).\nНажмите кнопку ниже для оплаты.",
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

def bot_autopinger():
    while True:
        time.sleep(300)
        if SELF_URL:
            try:
                print("[BOT AUTO-PING] Пингуем:", SELF_URL)
                requests.get(SELF_URL, timeout=10)
            except Exception as e:
                print("[BOT AUTO-PING] Ошибка пинга:", e)
        else:
            print("[BOT AUTO-PING] SELF_URL не установлен. Ожидание...")

def run_bot_autopinger():
    thread = threading.Thread(target=bot_autopinger, daemon=True)
    thread.start()

async def main():
    run_bot_autopinger()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
