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
    # Fetch transactions and balances
    tx_url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    txs = safe_request(tx_url) or []
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9

    tokens = {}
    # Process each transaction
    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        fee = tx.get('fee', 0) / 1e9
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            direction = ('buy' if tr.get('toUserAccount') == wallet else 
                         'sell' if tr.get('fromUserAccount') == wallet else None)
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

            if direction == 'buy':
                rec['buys'] += 1
                rec['in_tokens'] += amount
                rec['spent_sol'] += fee
                if not rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['out_tokens'] += amount
                rec['earned_sol'] += fee
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)

            rec['fee'] += fee

    # Final metrics
    for rec in tokens.values():
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol'] / rec['spent_sol'] * 100
                            if rec['spent_sol'] else 0)
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': (sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0) /
                        max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0))),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (sum(r['delta_sol'] for r in tokens.values()) /
                           (balance - sum(r['delta_sol'] for r in tokens.values()) ) * 100
                           if balance else 0),
        'winrate': (sum(1 for r in tokens.values() if r['delta_sol'] > 0) /
                    max(1, sum(1 for r in tokens.values() if abs(r['delta_sol']) > 0)) * 100),
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary


def generate_excel(wallet, tokens, summary):
    # Prepare filename
    filename = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"

    # Summary header
    headers = [
        'Wallet', 'WinRate', 'PnL R', 'Avg Win %', 'PnL Loss',
        'Balance change', 'TimePeriod', 'SOL Price Now', 'Balance'
    ]
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
        f"{summary['balance']:.2f} SOL"
    ]
    for col, val in enumerate(values, start=1):
        ws.cell(row=2, column=col, value=val)

    # Entry MCAP ranges
    ws.cell(row=4, column=1, value='Tokens entry MCAP:')
    ranges = ['<5k', '5k-30k', '30k-100k', '100k-300k', '300k+']
    for idx, rng in enumerate(ranges, start=2):
        ws.cell(row=5, column=idx, value=rng)

    # Table headers
    cols = [
        'Token', 'Spent SOL', 'Earned SOL', 'Delta Sol', 'Delta %', 'Buys',
        'Sells', 'Last trade', 'Income', 'Outcome', 'Fee', 'Period',
        'First buy Mcap', 'Last tx Mcap', 'Current Mcap', 'Contract',
        'Dexscreener', 'Photon'
    ]
    header_row = 8
    for col, title in enumerate(cols, start=1):
        ws.cell(row=header_row, column=col, value=title)

    # Fill table
    row = 9
    for rec in tokens.values():
        ws.cell(row=row, column=1, value=rec['symbol'])
        ws.cell(row=row, column=2, value=f"{rec['spent_sol']:.2f} SOL")
        ws.cell(row=row, column=3, value=f"{rec['earned_sol']:.2f} SOL")
        ws.cell(row=row, column=4, value=f"{rec['delta_sol']:.2f}")
        ws.cell(row=row, column=5, value=f"{rec['delta_pct']:.2f}%")
        ws.cell(row=row, column=6, value=rec['buys'])
        ws.cell(row=row, column=7, value=rec['sells'])
        # Last trade date
        if rec['last_trade']:
            ws.cell(row=row, column=8, value=rec['last_trade'].strftime('%d.%m.%Y'))
        # Token amounts
        ws.cell(row=row, column=9, value=rec['in_tokens'])
        ws.cell(row=row, column=10, value=rec['out_tokens'])
        ws.cell(row=row, column=11, value=f"{rec['fee']:.2f}")
        ws.cell(row=row, column=12, value=rec['period'])
        ws.cell(row=row, column=13, value=rec['first_mcap'])
        ws.cell(row=row, column=14, value=rec['last_mcap'])
        ws.cell(row=row, column=15, value=rec['current_mcap'])
        ws.cell(row=row, column=16, value=rec['mint'])
        # Hyperlinks
        cell_dex = ws.cell(row=row, column=17)
        cell_dex.value = 'View trades'
        cell_dex.hyperlink = f'https://dexscreener.com/solana/{rec['mint']}?maker={wallet}'
        cell_photo = ws.cell(row=row, column=18)
        cell_photo.value = 'View trades'
        cell_photo.hyperlink = f'https://photon-sol.tinyastro.io/en/lp/{rec['mint']}'
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
    bot.remove_webhook()
    bot.infinity_polling()
