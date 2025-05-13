import os
import requests
import telebot
from openpyxl import Workbook
from datetime import datetime
import threading
import http.server
import socketserver

# API ключи
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Получение транзакций от Helius
def get_token_transfers(wallet):
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100&parsedTransactionHistory=true"
    response = requests.get(url)
    if response.status_code != 200:
        return []

    txs = response.json()
    token_data = []

    for tx in txs:
        if tx.get("type") == "SWAP":
            timestamp = datetime.fromtimestamp(tx.get("timestamp", 0))
            swap_data = tx.get("events", {}).get("swap")

            if isinstance(swap_data, dict):
                native_input = swap_data.get("nativeInput") or []
                native_output = swap_data.get("nativeOutput") or []

                for event in native_input:
                    if isinstance(event, dict):
                        token_data.append({
                            "Token": event.get("mint", "Unknown"),
                            "Amount": event.get("amount", 0),
                            "Direction": "Buy",
                            "Date": timestamp.strftime("%Y-%m-%d %H:%M")
                        })

                for event in native_output:
                    if isinstance(event, dict):
                        token_data.append({
                            "Token": event.get("mint", "Unknown"),
                            "Amount": event.get("amount", 0),
                            "Direction": "Sell",
                            "Date": timestamp.strftime("%Y-%m-%d %H:%M")
                        })

    return token_data

# Excel генерация
def generate_excel(wallet, data):
    wb = Workbook()
    ws = wb.active
    ws.title = "Token Activity"
    ws.append(["Token", "Amount", "Direction", "Date"])

    for row in data:
        ws.append([row["Token"], row["Amount"], row["Direction"], row["Date"]])

    filename = f"{wallet}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(filename)
    return filename

# Обработка адреса
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Отправь мне адрес Solana-кошелька.")

@bot.message_handler(func=lambda message: True)
def handle_wallet(message):
    wallet = message.text.strip()
    if len(wallet) in [32, 44] and wallet.isalnum():
        bot.reply_to(message, "Формирую отчёт...")

        data = get_token_transfers(wallet)
        if not data:
            bot.send_message(message.chat.id, "Не удалось получить данные или операций не найдено.")
            return

        excel_file = generate_excel(wallet, data)
        with open(excel_file, "rb") as f:
            bot.send_document(message.chat.id, f)
        os.remove(excel_file)
    else:
        bot.reply_to(message, "Пожалуйста, отправь корректный адрес Solana.")

# Запуск polling и фейкового веб-сервера
threading.Thread(target=bot.polling, daemon=True).start()

PORT = 10000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Render порт открыт (PORT {PORT}) — бот запущен.")
    httpd.serve_forever()
