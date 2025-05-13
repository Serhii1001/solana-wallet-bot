import telebot
import os
from openpyxl import Workbook
from datetime import datetime

# Telegram API
TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

# Генерация Excel-отчёта
def generate_excel(wallet_address):
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws["A1"] = f"Отчёт для: {wallet_address}"
    filename = f"{wallet_address}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(filename)
    return filename

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Отправь мне адрес Solana-кошелька.")

@bot.message_handler(func=lambda message: True)
def handle_address(message):
    wallet = message.text.strip()
    if len(wallet) in [32, 44] and all(c.isalnum() for c in wallet):
        bot.reply_to(message, "Адрес получен, формирую отчёт...")
        file_path = generate_excel(wallet)
        with open(file_path, "rb") as f:
            bot.send_document(message.chat.id, f)
        os.remove(file_path)
    else:
        bot.reply_to(message, "Пожалуйста, отправь корректный адрес Solana.")

# Запуск polling в отдельном потоке
import threading
threading.Thread(target=bot.polling, daemon=True).start()

# Фейковый веб-сервер для Render
import http.server
import socketserver
PORT = 10000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Render порт открыт (PORT {PORT}) — бот запущен.")
    httpd.serve_forever()
