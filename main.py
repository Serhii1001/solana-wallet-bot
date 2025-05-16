import os
import json
import requests
import telebot
from datetime import datetime
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE = os.getenv("SOL_PRICE", "0")

# Initialize Telegram bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)


def safe_request(url, params=None):
    for _ in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return {}


def get_symbol(mint):
    url = f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}"
    data = safe_request(url)
    return data.get("symbol", mint)


def get_historical_mcap(mint, ts_dt):
    url = f"{DEXSCREENER_BASE}{mint}/chart?interval=1h"
    data = safe_request(url)
    points = data.get('chart', [])
    if not points:
        return ""
    target = int(ts_dt.timestamp() * 1000)
    best = min(points, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', "")


def get_current_mcap(mint):
    data = safe_request(DEXSCREENER_BASE + mint)
    return data.get('stats', {}).get('marketCap', "")


def format_duration(start, end):
    if not start or not end:
        return "-"
    delta = end - start
    seconds = delta.total_seconds()
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{int(days)}d {int(hours)}h"
    if hours:
        return f"{int(hours)}h {int(minutes)}m"
    if minutes:
        return f"{int(minutes)}m"
    return f"{int(sec)}s"


def analyze_wallet(wallet):
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
        # Calculate actual SOL spent/earned
        sol_spent = sol_earned = 0.0
        for nt in tx.get('nativeTransfers', []):
            lamports = nt.get('amount', 0)
            sol = lamports / 1e9
            if nt.get('fromUserAccount') == wallet:
                sol_spent += sol
            if nt.get('toUserAccount') == wallet:
                sol_earned += sol
        per_tx_seen = set()

        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            direction = (
                'buy' if tr.get('toUserAccount') == wallet else
                'sell' if tr.get('fromUserAccount') == wallet else None
            )
            if not direction:
                continue

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

            key = (mint, direction)
            if key not in per_tx_seen:
                if direction == 'buy':
                    rec['buys'] += 1
                    rec['spent_sol'] += sol_spent
                else:
                    rec['sells'] += 1
                    rec['earned_sol'] += sol_earned
                per_tx_seen.add(key)

            if direction == 'buy':
                rec['in_tokens'] += amount
                if not rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['out_tokens'] += amount
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
            sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0) /
            max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0))
        ),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (
            sum(r['delta_sol'] for r in tokens.values()) /
            ((balance - sum(r['delta_sol'] for r in tokens.values())) or 1) * 100
            if balance else 0
        ),
        'winrate': (
            sum(1 for r in tokens.values() if r['delta_sol'] > 0) /
            max(1, sum(1 for r in tokens.values() if abs(r['delta_sol']) > 0)) * 100
        ),
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary


def generate_excel(wallet, tokens, summary):
    filename = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"

    # Header summary
    headers = ['Wallet', 'WinRate', 'PnL R', 'Avg Win %', 'PnL Loss',
               'Balance change', 'TimePeriod', 'SOL Price Now', 'Balance']
    for col, title in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=title)
    values = [
        wallet,
        f"{summary['winrate']:.2f}%",
        f"{summary['pnl']:.2f} SOL",
        f"{summary['avg_win_pct']:.2f}%",
        f"{summary['pnl_loss']:.2f} SOL",
        f"{summary['balance_change']:.2f}%",
        summary['time_period'],
        f"{summary['sol_price']} $",
        f"{summary['balance']:.2f} SOL",
    ]
    for col, val in enumerate(values, start=1):
        ws.cell(row=2, column=col, value=val)

    # MCAP ranges
    ws.cell(row=4, column=1, value='Tokens entry MCAP:')
    ranges = ['<5k', '5k-30k', '30k-100k', '100k-300k', '300k+']
    for idx, rng in enumerate(ranges, start=2):
        ws.cell(row=5, column=idx, value=rng)

    # Table headers
    cols = ['Token', 'Spent SOL', 'Earned SOL', 'Delta Sol', 'Delta %', 'Buys',
            'Sells', 'Last trade', 'Income', 'Outcome', 'Fee', 'Period',
            'First buy Mcap', 'Last tx Mcap', 'Current Mcap', 'Contract',
            'Dexscreener', 'Photon']
    for col, title in enumerate(cols, start=1):
        ws.cell(row=8, column=col, value=title)

    # Fill rows
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
        cd = ws.cell(row=row, column=17)
        cd.value = 'View trades'
        cd.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        cp = ws.cell(row=row, column=18)
        cp.value = 'View trades'
        cp.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        row += 1

    wb.save(filename)
    return filename

@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "Привет! Отправь Solana-адрес.")

@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    bot.reply_to(message, "Обрабатываю...")
    tokens, summary = analyze_wallet(wallet)
    fname = generate_excel(wallet, tokens, summary)
    with open(fname, "rb") as f:
        bot.send_document(message.chat.id, f)

if __name__ == "__main__":
    # Ensure no other getUpdates listeners or webhooks are active
    bot.remove_webhook()
    # Start polling for Telegram messages
    bot.infinity_polling()
