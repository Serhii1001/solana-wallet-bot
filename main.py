
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
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Получение списка транзакций по адресу
def get_transaction_signatures(wallet):
    url = "https://api.mainnet-beta.solana.com"
    headers = {"Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            wallet,
            {"limit": 10}
        ]
    }
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    if response.status_code != 200:
        return []

    return [tx["signature"] for tx in response.json().get("result", [])]

# Получение и разбор транзакции
def get_token_transfers(wallet):
    signatures = get_transaction_signatures(wallet)
    url = "https://api.mainnet-beta.solana.com"
    headers = {"Content-Type": "application/json"}
    result_data = []

    for sig in signatures:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getParsedTransaction",
            "params": [
                sig,
                {"encoding": "jsonParsed"}
            ]
        }
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        if response.status_code != 200:
            continue

        parsed = response.json().get("result")
        if not parsed:
            continue

        block_time = parsed.get("blockTime")
        date_str = datetime.fromtimestamp(block_time).strftime("%Y-%m-%d %H:%M") if block_time else "n/a"

        instructions = parsed.get("transaction", {}).get("message", {}).get("instructions", [])
        for ix in instructions:
            program = ix.get("program")
            parsed_ix = ix.get("parsed", {})
            if program == "spl-token" and isinstance(parsed_ix, dict):
                info = parsed_ix.get("info", {})
                amount = info.get("amount")
                source = info.get("source")
                destination = info.get("destination")
                mint = info.get("mint")

                result_data.append({
                    "Token": mint,
                    "Amount": amount,
                    "From": source,
                    "To": destination,
                    "Date": date_str
                })

    return result_data

# Генерация Excel
def generate_excel(wallet, data):
    from openpyxl import Workbook
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
