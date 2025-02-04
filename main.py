# main.py
import threading
import asyncio
from payment_api import app  # Импорт Flask-сервера
from bot import main as bot_main  # Импорт основной функции бота из bot.py

def run_flask_server():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    asyncio.run(bot_main())
