
import os
import json
import requests
import telebot
from openpyxl import Workbook
from datetime import datetime
import threading
import http.server
import socketserver

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Получение транзакций через Helius Enhanced API
def get_helius_transactions(wallet):
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=50"
    response = requests.get(url)

    with open("debug_helius.txt", "w") as f:
        f.write("🧪 Helius Response:\n")
        f.write(response.text)

    if response.status_code != 200:
        return []

    return response.json()

# Обработка транзакций и формирование данных
def get_token_transfers(wallet):
    transactions = get_helius_transactions(wallet)
    result_data = []

    for tx in transactions:
        timestamp = tx.get("timestamp")
        date_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "n/a"
        events = tx.get("events", {})
        transfers = events.get("tokenTransfers", [])

        for transfer in transfers:
            result_data.append({
                "Token": transfer.get("mint", "Unknown"),
                "Amount": transfer.get("amount", 0),
                "From": transfer.get("fromUserAccount", "n/a"),
                "To": transfer.get("toUserAccount", "n/a"),
                "Date": date_str
            })

    return result_data

# Генерация Excel
def generate_excel(wallet, data):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Token", "Amount", "From", "To", "Date"])

    for row in data:
        ws.append([row["Token"], row["Amount"], row["From"], row["To"], row["Date"]])

    filename = f"{wallet}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(filename)
    return filename

# Команды Telegram
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Отправь мне адрес Solana-кошелька.")

@bot.message_handler(func=lambda message: True)
def handle_wallet(message):
    wallet = message.text.strip()
    if len(wallet) in [32, 44] and wallet.isalnum():
        bot.reply_to(message, "Формирую отчёт...")
        data = get_token_transfers(wallet)

        # Отправка debug_helius.txt
        try:
            with open("debug_helius.txt", "rb") as f:
                bot.send_document(message.chat.id, f)
        except:
            bot.send_message(message.chat.id, "⚠️ Не удалось отправить debug_helius.txt")

        if not data:
            bot.send_message(message.chat.id, "Не удалось получить данные или операций не найдено.")
            return

        excel_file = generate_excel(wallet, data)
        with open(excel_file, "rb") as f:
            bot.send_document(message.chat.id, f)
        os.remove(excel_file)
    else:
        bot.reply_to(message, "Пожалуйста, отправь корректный адрес Solana.")

# Фейковый веб-сервер + polling
threading.Thread(target=bot.polling, daemon=True).start()

PORT = 10000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Бот запущен и слушает PORT {PORT}")
    httpd.serve_forever()
