import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook
from bs4 import BeautifulSoup

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
PHOTON_BASE = "https://photon-sol.tinyastro.io/en/lp/"
SOL_PRICE = os.getenv("SOL_PRICE", "0")

# Initialize bot and Flask app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)


def safe_request(url, params=None, headers=None, timeout=10):
    """
    Простая обертка для GET-запросов, возвращающая JSON или None в случае ошибки
    """
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Request error ({url}): {e}")
        return None


def analyze_wallet(wallet):
    """
    Собирает токен-трансферы через Helius и перекладывает расчёт spent_sol на DEX-источники
    """
    # Получаем транзакции из Helius
    helius_url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}"
    txs = safe_request(helius_url) or []

    tokens = {}
    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            if not mint:
                continue
            rec = tokens.setdefault(mint, {
                'mint': mint,
                'earned_sol': 0.0
            })
            # Увеличиваем earned_sol для входящих токенов
            if tr.get('toUserAccount') == wallet:
                rec['earned_sol'] += tr.get('amount', 0) / 1e9

    # Пересчёт spent_sol через DEX-сервисы
    for rec in tokens.values():
        mint = rec['mint']
        rec['spent_sol'] = get_spent_via_dexscreener(mint, wallet)
        rec['spent_sol_photon'] = get_spent_via_photon(mint, wallet)
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']

    return list(tokens.values())


def get_spent_via_dexscreener(mint, wallet):
    """
    Запрашивает историю свопов у Dexscreener и возвращает сумму SOL,
    потраченную указанным кошельком на покупку данного токена
    """
    url = f"{DEXSCREENER_BASE}{mint}/trades?maker={wallet}"
    data = safe_request(url) or {}
    total = 0.0
    for t in data.get('trades', []):
        if t.get('baseToken') == 'SOL':
            total += float(t.get('baseAmount', 0))
    return total


def get_spent_via_photon(mint, wallet):
    """
    Парсит HTML Photon LP и возвращает сумму SOL,
    потраченную кошельком на покупку токена
    """
    url = f"{PHOTON_BASE}{mint}"
    try:
        html = requests.get(url, timeout=10).text
        soup = BeautifulSoup(html, 'html.parser')
        total = 0.0
        # Ищем таблицу или элементы с торговыми данными
        rows = soup.select('table tr')
        for row in rows:
            cols = [c.get_text(strip=True) for c in row.find_all('td')]
            if len(cols) < 4:
                continue
            maker_addr = cols[1]
            token_in = cols[2]
            amount_in = cols[3]
            # Предполагаем, что SOL всегда первый токен в паре
            if maker_addr.lower() == wallet.lower() and 'SOL' in token_in:
                # Убираем возможные запятые и конвертим
                val = float(amount_in.replace(',', ''))
                total += val
        return total
    except Exception as e:
        print(f"Photon parse error ({url}): {e}")
        return 0.0


def build_xlsx(wallet, tokens, summary):
    wb = Workbook()
    ws = wb.active
    # Заголовки столбцов
    headers = ['Mint', 'Earned SOL', 'Spent SOL (DEX)', 'Spent SOL (Photon)', 'Delta SOL', 'View trades']
    for idx, h in enumerate(headers, 1):
        ws.cell(1, idx).value = h

    r = 2
    for rec in tokens:
        ws.cell(r, 1).value = rec['mint']
        ws.cell(r, 2).value = rec['earned_sol']
        ws.cell(r, 3).value = rec.get('spent_sol', 0.0)
        ws.cell(r, 4).value = rec.get('spent_sol_photon', 0.0)
        ws.cell(r, 5).value = rec.get('delta_sol', 0.0)
        # Ссылка на просмотр трейдов
        cell = ws.cell(r, 6)
        cell.value = 'View trades'
        cell.hyperlink = f"{PHOTON_BASE}{rec['mint']}"
        r += 1

    fname = f"report_{wallet}_{int(datetime.now().timestamp())}.xlsx"
    wb.save(fname)
    return fname


# Handlers

def welcome(m):
    bot.reply_to(m, "Привет! Отправь Solana-адрес.")

bot.register_message_handler(welcome, commands=['start'])


def handle(m):
    wallet = m.text.strip()
    bot.reply_to(m, "Обрабатываю кошелек, чуть позже пришлю отчет...")
    tokens = analyze_wallet(wallet)
    summary = None  # при необходимости соберите сводку
    report = build_xlsx(wallet, tokens, summary)
    bot.send_document(m.chat.id, open(report, 'rb'))

bot.register_message_handler(handle, func=lambda _: True)


# Webhook setup for Render
@app.route('/webhook', methods=['POST'])
def webhook():
    json_data = request.get_json(force=True)
    bot.process_new_updates([telebot.types.Update.de_json(json_data)])
    return '', 200


def main():
    # Устанавливаем webhook
    if WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))


if __name__ == '__main__':
    main()
