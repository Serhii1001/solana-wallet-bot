import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

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
    return safe_request(f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}").get('symbol', mint)

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

# Core logic

def analyze_wallet(wallet):
    bal = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}"
    )
    balance = bal.get('nativeBalance', 0) / 1e9
    tokens = {}

    # We'll fetch trades from Dexscreener for each token
    # First, gather unique mints from Helius tokenTransfers
    txs = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    ) or []
    mints = set()
    for tx in txs:
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            if tr.get('toUserAccount') == wallet or tr.get('fromUserAccount') == wallet:
                mints.add(mint)

    # For each mint, query Dexscreener trades API
    for mint in mints:
        rec = tokens.setdefault(mint, {
            'mint': mint,
            'symbol': get_symbol(mint),
            'spent_sol': 0,
            'earned_sol': 0,
            'delta_sol': 0,
            'delta_pct': 0,
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
        # Fetch trades
        data = safe_request(
            f"https://api.dexscreener.com/latest/dex/trades/solana/{mint}?maker={wallet}"
        )
        trades = data.get('trades', [])
        for t in trades:
            side = t.get('side')  # 'buy' or 'sell'
            ts = datetime.fromtimestamp(t.get('timestamp') / 1000)
            amt_token = float(t.get('amount', 0))
            amt_quote = float(t.get('amountQuote', 0)) / 1e9  # SOL quote
            # Update rec
            if side == 'buy':
                rec['buys'] += 1
                rec['spent_sol'] += amt_quote
                rec['in_tokens'] += amt_token
                if not rec['first_ts'] or ts < rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['earned_sol'] += amt_quote
                rec['out_tokens'] += amt_token
                if not rec['last_ts'] or ts > rec['last_ts']:
                    rec['last_ts'] = ts
                    rec['last_mcap'] = get_historical_mcap(mint, ts)

        rec['fee'] = 0  # Dexscreener data doesn't include fee
        rec['current_mcap'] = get_current_mcap(mint)
        # Final metrics
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol'] / rec['spent_sol'] * 100) if rec['spent_sol'] else 0
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']

    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': sum(r['delta_pct'] for r in tokens.values() if r['delta_sol']>0) / max(1, sum(1 for r in tokens.values() if r['delta_sol']>0)),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol']<0),
        'balance_change': sum(r['delta_sol'] for r in tokens.values()) / ((balance - sum(r['delta_sol'] for r in tokens.values())) or 1) * 100,
        'winrate': sum(1 for r in tokens.values() if r['delta_sol']>0) / max(1, sum(1 for r in tokens.values() if abs(r['delta_sol'])>0)) * 100,
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary

# Generate Excel report

def generate_excel(wallet, tokens, summary):
    fn = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"

    hdr = ['Wallet','WinRate','PnL R','Avg Win %','PnL Loss','Balance change','TimePeriod','SOL Price Now','Balance']
    for i, t in enumerate(hdr, 1):
        ws.cell(row=1, column=i, value=t)

    vals = [
        wallet,
        f"{summary['winrate']:.2f}%",
        f"{summary['pnl']:.2f} SOL",
        f"{summary['avg_win_pct']:.2f}%",
        f"{summary['pnl_loss']:.2f} SOL",
        f"{summary['balance_change']:.2f}%",
        summary['time_period'],
        f"{summary['sol_price']} $",
        f"{summary['balance']:.2f} SOL"
    ]
    for i, v in enumerate(vals, 1):
        ws.cell(row=2, column=i, value=v)

    ws.cell(row=4, column=1, value='Tokens entry MCAP:')
    ranges = ['<5k','5k-30k','30k-100k','100k-300k','300k+']
    for i, r in enumerate(ranges, 2):
        ws.cell(row=5, column=i, value=r)

    cols = ['Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells','Last trade','Income','Outcome','Fee','Period','First buy Mcap','Last tx Mcap','Current Mcap','Contract','Dexscreener','Photon']
    for i, c in enumerate(cols, 1):
        ws.cell(row=8, column=i, value=c)

    row = 9
    for rec in tokens.values():
        ws.cell(row=row, column=1, value=rec['symbol'])
        ws.cell(row=row, column=2, value=f"{rec['spent_sol']:.2f} SOL")
        ws.cell(row=row, column=3, value=f"{rec['earned_sol']:.2f} SOL")
        ws.cell(row=row, column=4, value=f"{rec['delta_sol']:.2f}")
        ws.cell(row=row, column=5, value=f"{rec['delta_pct']:.2f}%")
        ws.cell(row=row, column=6, value=rec['buys'])
        ws.cell(row=row, column=7, value=rec['sells'])
        if rec['last_trade']:
            ws.cell(row=row, column=8, value=rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(row=row, column=9, value=rec['in_tokens'])
        ws.cell(row=row, column=10, value=rec['out_tokens'])
        ws.cell(row=row, column=11, value=f"{rec['fee']:.2f}")
        ws.cell(row=row, column=12, value=rec['period'])
        ws.cell(row=row, column=13, value=rec['first_mcap'])
        ws.cell(row=row, column=14, value=rec['last_mcap'])
        ws.cell(row=row, column=15, value=rec['current_mcap'])
        ws.cell(row=row, column=16, value=rec['mint'])

        cell_dex = ws.cell(row=row, column=17)
        cell_dex.value = 'View trades'
        cell_dex.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"

        cell_ph = ws.cell(row=row, column=18)
        cell_ph.value = 'View trades'
        cell_ph.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"

        row += 1

    wb.save(fn)
    return fn
