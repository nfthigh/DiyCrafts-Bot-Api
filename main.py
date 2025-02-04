# main.py
import threading
import asyncio
from payment_api import app, run_autopinger_thread  # Импорт Flask‑сервера и автопинга
from bot import main as bot_main  # Импорт основной функции бота из bot.py

def run_flask_server():
    # Запускаем автопинг для Flask‑сервера
    run_autopinger_thread()
    app.run(host="0.0.0.0", port=5000, debug=True)

if __name__ == '__main__':
    # Запускаем Flask‑сервер в отдельном потоке
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    # Запускаем Telegram‑бота (асинхронно)
    asyncio.run(bot_main())
