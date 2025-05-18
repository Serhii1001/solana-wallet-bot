import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime, timedelta
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE = os.getenv("SOL_PRICE", "0")

# Initialize bot and app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Configure webhook (remove existing, then set new)
bot.remove_webhook()
bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# Health-check endpoint
def health_check():
    return "OK", 200
app.add_url_rule('/', 'health_check', health_check, methods=['GET'])

# Telegram webhook endpoint
def telegram_webhook():
    data = request.get_data(as_text=True)
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return "OK", 200
app.add_url_rule(f'/{TELEGRAM_TOKEN}', 'telegram_webhook', telegram_webhook, methods=['POST'])

# HTTP helper
def safe_request(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            continue
    return {}

# Helpers
def get_symbol(mint):
    return safe_request(
        f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}"
    ).get('symbol', mint)

def get_historical_mcap(mint, ts):
    chart = safe_request(f"{DEXSCREENER_BASE}{mint}/chart?interval=1h").get('chart', [])
    if not chart:
        return ''
    target = int(ts.timestamp() * 1000)
    best = min(chart, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', '')

def get_current_mcap(mint):
    return safe_request(f"{DEXSCREENER_BASE}{mint}").get('stats', {}).get('marketCap', '')

def format_duration(start, end):
    if not start or not end:
        return '-'
    delta = end - start
    days, rem = divmod(delta.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{int(days)}d {int(hours)}h"
    if hours:
        return f"{int(hours)}h {int(minutes)}m"
    if minutes:
        return f"{int(minutes)}m"
    return f"{int(seconds)}s"

# Fetch all transactions within last 30 days
def fetch_transactions(wallet, days=30):
    all_txs = []
    before = None
    cutoff = datetime.utcnow() - timedelta(days=days)
    while True:
        params = {"api-key": HELIUS_API_KEY, "limit": 100}
        if before:
            params["before"] = before
        page = safe_request(
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
            params
        ) or []
        if not page:
            break
        for tx in page:
            ts = datetime.fromtimestamp(tx.get('timestamp', 0))
            # Stop if transaction is older than cutoff
            if ts < cutoff:
                return all_txs
            all_txs.append(tx)
        # If less than page size, no more newer transactions
        if len(page) < 100:
            break
        before = page[-1].get("signature")
    return all_txs

# Core logic

def analyze_wallet(wallet):
    # Получаем все транзакции за последние 30 дней
    txs = fetch_transactions(wallet, days=30)
    bal = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}"
    )
    balance = bal.get('nativeBalance', 0) / 1e9
    tokens = {}

    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        sol_spent = sol_earned = 0.0

        # SOL transfers
        for nt in tx.get('nativeTransfers', []):
            amount = nt.get('amount', 0) / 1e9
            if nt.get('fromUserAccount') == wallet:
                sol_spent += amount
            if nt.get('toUserAccount') == wallet:
                sol_earned += amount

        # Token transfers
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amt = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            direction = (
                'buy'  if tr.get('toUserAccount') == wallet else
                'sell' if tr.get('fromUserAccount') == wallet else
                None
            )
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
                rec['in_tokens'] += amt
                rec['spent_sol'] += sol_spent
                if not rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['out_tokens'] += amt
                rec['earned_sol'] += sol_earned
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)

            rec['fee'] += tx.get('fee', 0) / 1e9

    for rec in tokens.values():
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (
            rec['delta_sol'] / rec['spent_sol'] * 100
            if rec['spent_sol'] else 0
        )
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': (
            sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0)
            / max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0))
        ),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (
            sum(r['delta_sol'] for r in tokens.values())
            / ((balance - sum(r['delta_sol'] for r in tokens.values())) or 1) * 100
        ),
        'winrate': (
            sum(1 for r in tokens.values() if r['delta_sol'] > 0)
            / max(1, sum(1 for r in tokens.values() if abs(r['delta_sol']) > 0)) * 100
        ),
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary

# Excel report and handlers remain the same
# ...
