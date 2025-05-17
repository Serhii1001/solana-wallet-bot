import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook

# Конфигурация
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например, https://ваш-домен/onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE = os.getenv("SOL_PRICE", "0")

# Инициализация бота и приложения
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Настройка вебхука (сначала удаляем существующий, затем устанавливаем новый)
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

# Безопасный HTTP-запрос с ретраями

def safe_request(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            continue
    return {}

# Получить символ токена по mint

def get_symbol(mint):
    return safe_request(
        f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}"
    ).get('symbol', mint)

# Получить историческую рыночную капитализацию на момент ts

def get_historical_mcap(mint, ts):
    chart = safe_request(f"{DEXSCREENER_BASE}{mint}/chart?interval=1h").get('chart', [])
    if not chart:
        return ''
    target = int(ts.timestamp() * 1000)
    closest = min(chart, key=lambda p: abs(p.get('timestamp', 0) - target))
    return closest.get('marketCap', '')

# Получить текущую рыночную капитализацию

def get_current_mcap(mint):
    return safe_request(f"{DEXSCREENER_BASE}{mint}").get('stats', {}).get('marketCap', '')

# Отформатировать длительность между двумя метками времени

def format_duration(start, end):
    if not start or not end:
        return '-'
    delta = end - start
    days, rem = divmod(delta.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{int(days)}д {int(hours)}ч"
    if hours:
        return f"{int(hours)}ч {int(minutes)}м"
    if minutes:
        return f"{int(minutes)}м"
    return f"{int(seconds)}с"

# Основная логика анализа кошелька

def analyze_wallet(wallet):
    # Запрос последних транзакций и баланса
    txs = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    ) or []
    bal = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}"
    )
    balance = bal.get('nativeBalance', 0) / 1e9
    tokens = {}

    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        # Сумма списания и зачисления SOL
        native_spent = sum(
            nt.get('amount', 0) / 1e9
            for nt in tx.get('nativeTransfers', [])
            if nt.get('fromUserAccount') == wallet
        )
        native_earned = sum(
            nt.get('amount', 0) / 1e9
            for nt in tx.get('nativeTransfers', [])
            if nt.get('toUserAccount') == wallet
        )
        # Вычитаем комиссии из потраченного SOL
        fee_native = tx.get('fee', 0) / 1e9
        net_spent = native_spent - fee_native

        # Покупки и продажи токенов в транзакции
        buys_per_mint = {}
        sells_per_mint = {}
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            if tr.get('toUserAccount') == wallet:
                buys_per_mint[mint] = buys_per_mint.get(mint, 0) + amount
            elif tr.get('fromUserAccount') == wallet:
                sells_per_mint[mint] = sells_per_mint.get(mint, 0) + amount

        # Распределение SOL по токенам
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            # Пропускаем wSOL обёртку/развёртку
            if mint == 'So11111111111111111111111111111111111111112':
                continue

            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            direction = 'buy' if tr.get('toUserAccount') == wallet else 'sell' if tr.get('fromUserAccount') == wallet else None
            if not direction:
                continue

            rec = tokens.setdefault(mint, {
                'mint': mint,
                'symbol': get_symbol(mint),
                'spent_sol': 0,
                'earned_sol': 0,
                'buys': 0,
                'sells': 0,
                'in_tokens': 0,
                'out_tokens': 0,
                'fee': 0,
                'first_ts': None,
                'last_ts': None,
                'first_mcap': '',
                'last_mcap': '',
                'current_mcap': ''
            })

            if direction == 'buy':
                rec['buys'] += 1
                rec['in_tokens'] += amount
                total_bought = buys_per_mint.get(mint, 1)
                proportion = amount / total_bought if total_bought else 0
                rec['spent_sol'] += net_spent * proportion
                if rec['first_ts'] is None:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['out_tokens'] += amount
                total_sold = sells_per_mint.get(mint, 1)
                proportion = amount / total_sold if total_sold else 0
                rec['earned_sol'] += native_earned * proportion
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)

            # Сборы сети на каждый токен
            rec['fee'] += fee_native * proportion if direction == 'buy' else fee_native * proportion

    # Итоговые показатели
    for rec in tokens.values():
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol'] / rec['spent_sol'] * 100) if rec['spent_sol'] else 0
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0) / max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0)),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (sum(r['delta_sol'] for r in tokens.values()) / ((balance - sum(r['delta_sol'] for r in tokens.values())) or 1) * 100),
        'winrate': sum(1 for r in tokens.values() if r['delta_sol'] > 0) / max(1, sum(1 for r in tokens.values() if abs(r['delta_sol']) > 0)) * 100,
        'time_period': '30 дней',
        'sol_price': SOL_PRICE
    }
    return tokens, summary

# Генерация Excel-отчета

... (остальной код без изменений)
