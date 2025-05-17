import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Конфигурация
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например, https://ваш-домен/onrender.com
# Базовые URLs для работы с Dexscreener
DEXSCREENER_TRADE_BASE = "https://api.dexscreener.com/latest/dex/trades/solana/"
# Цена SOL в USD, нужна для конвертации объема в USD при необходимости
SOL_PRICE = float(os.getenv("SOL_PRICE", "0"))

# Инициализация бота и Flask приложения
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Настройка вебхука (удаляем старый, ставим новый)
bot.remove_webhook()
bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# Эндпоинт для проверки статуса
@app.route('/', methods=['GET'])
def health_check():
    return "OK", 200

# Эндпоинт для Telegram вебхука
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.get_data(as_text=True)
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return "OK", 200

# Функция для безопасных HTTP-запросов с ретраями
def safe_get(url, params=None, headers=None):
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    response = session.get(url, params=params, headers=headers or {})
    response.raise_for_status()
    return response

# Получение общего объема покупок в SOL по указанной паре Dexscreener
def get_spend_sol(pair_id):
    try:
        url = DEXSCREENER_TRADE_BASE + pair_id
        resp = safe_get(url)
        data = resp.json()
        trades = data.get('trades', [])
        total_sol = 0.0
        for t in trades:
            # Токен обмена: quoteToken — SOL, baseToken — целевой токен
            if t.get('side') == 'buy':
                sol_amount = float(t.get('quoteTokenAmount', 0))
                total_sol += sol_amount
        return total_sol
    except Exception as e:
        print(f"Error fetching Dexscreener data: {e}")
        return 0.0

# Анализ кошелька через Helius и подсчет метрик
def analyze_wallet(address, dex_pair_id=None):
    # Запрос списка транзакций Solana по адресу
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={HELIUS_API_KEY}"
    resp = safe_get(url)
    txs = resp.json()

    sent = 0.0
    received = 0.0
    total_tx = len(txs)

    for tx in txs:
        # Проходим по инструкциям в каждой транзакции
        for instruction in tx.get('instructions', []):
            info = instruction.get('info', {})
            # Учитываем SOL-торговые инстанции (transfer)
            if instruction.get('type') == 'transfer':
                lamports = info.get('lamports', 0)
                sol_amount = lamports / 1e9
                # Источник транзакции — наш адрес
                if info.get('source') == address:
                    sent += sol_amount
                # Назначение транзакции — наш адрес
                if info.get('destination') == address:
                    received += sol_amount

    # Подсчет объема торгов через Dexscreener (если передан dex_pair_id)
    spend_sol = get_spend_sol(dex_pair_id) if dex_pair_id else 0.0

    return {
        'address': address,
        'total_transactions': total_tx,
        'sent_sol': sent,
        'received_sol': received,
        'spend_sol': spend_sol
    }

# Обработка команды /analyze
@bot.message_handler(commands=['analyze'])
def cmd_analyze(message):
    parts = message.text.split()
    if len(parts) not in (2, 3):
        bot.reply_to(message, "Использование: /analyze <wallet_address> [dex_pair_id]")
        return

    address = parts[1]
    dex_pair_id = parts[2] if len(parts) == 3 else None

    bot.send_message(message.chat.id, "Запускаю анализ, пожалуйста подождите...")
    stats = analyze_wallet(address, dex_pair_id)

    # Формируем Excel-отчет
    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Analysis"
    ws.append(["Параметр", "Значение"])
    for key, value in stats.items():
        ws.append([key, value])

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"wallet_analysis_{address}_{timestamp}.xlsx"
    wb.save(filename)

    # Отправляем файл пользователю
    with open(filename, 'rb') as doc:
        bot.send_document(message.chat.id, doc)

# Запуск Flask-приложения
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
