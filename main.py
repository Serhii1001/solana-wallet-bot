import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL")  # https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

# Initialize bot and Flask app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Remove webhook if exists and set new one
bot.delete_webhook()
bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# Helper to make HTTP GET requests with retries
def safe_request(url, params=None):
    for _ in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            continue
    return {}

# Helpers to fetch data

def get_symbol(mint):
    data = safe_request(f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}")
    return data.get("symbol", mint)


def format_duration(start, end):
    if not start or not end:
        return "-"
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

# Core logic: Analyze wallet via Helius transactions to match Dexscreener
# We count each tokenTransfer as a separate trade
# and use nativeTransfers to compute SOL spent/earned per tx
def analyze_wallet(wallet):
    # Fetch transactions and balance
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
        # Compute SOL spent/earned this tx
        sol_spent = sol_earned = 0.0
        for nt in tx.get('nativeTransfers', []):
            lam = nt.get('amount', 0)
            sol = lam / 1e9
            if nt.get('fromUserAccount') == wallet:
                sol_spent += sol
            if nt.get('toUserAccount') == wallet:
                sol_earned += sol
        # For each token transfer, count buy or sell
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            if tr.get('toUserAccount') == wallet:
                direction = 'buy'
            elif tr.get('fromUserAccount') == wallet:
                direction = 'sell'
            else:
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
            # Update counts and amounts
            if direction == 'buy':
                rec['buys'] += 1
                rec['spent_sol'] += sol_spent
                rec['in_tokens'] += amount
                if rec['first_ts'] is None:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['earned_sol'] += sol_earned
                rec['out_tokens'] += amount
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)
            # Accumulate fee
            rec['fee'] += tx.get('fee', 0) / 1e9

    # Finalize metrics
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
        'avg_win_pct': (
            sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0) /
            max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0))
        ),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (
            sum(r['delta_sol'] for r in tokens.values()) /
            max(1, balance) * 100
        ),
        'winrate': (
            sum(1 for r in tokens.values() if r['delta_sol'] > 0) /
            max(1, len(tokens)) * 100
        ),
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary

# Generate Excel report
def generate_excel(wallet, tokens, summary):
    filename = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"

    # Summary headers
    hdr_summary = ['Wallet', 'WinRate', 'PnL R', 'Avg Win %', 'PnL Loss',
                   'Balance change', 'TimePeriod', 'SOL Price Now', 'Balance']
    for col, title in enumerate(hdr_summary, start=1):
        ws.cell(row=1, column=col, value=title)

    # Summary values
    summary_vals = [
        wallet,
        f"{summary['winrate']:.2f}%",
        f"{summary['pnl']:.4f} SOL",
        f"{summary['avg_win_pct']:.2f}%",
        f"{summary['pnl_loss']:.4f} SOL",
        f"{summary['balance_change']:.2f}%",
        summary['time_period'],
        f"{summary['sol_price']} $",
        f"{summary['balance']:.4f} SOL"
    ]
    for col, val in enumerate(summary_vals, start=1):
        ws.cell(row=2, column=col, value=val)

    # MCAP ranges
    ws.cell(row=4, column=1, value='Tokens entry MCAP:')
    ranges = ['<5k', '5k-30k', '30k-100k', '100k-300k', '300k+']
    for idx, rng in enumerate(ranges, start=2):
        ws.cell(row=5, column=idx, value=rng)

    # Table headers
    table_headers = ['Token', 'Spent SOL', 'Earned SOL', 'Delta Sol', 'Delta %',
                     'Buys', 'Sells', 'Last trade', 'Income', 'Outcome', 'Fee',
                     'Period', 'First buy Mcap', 'Last tx Mcap', 'Current Mcap',
                     'Contract', 'Dexscreener', 'Photon']
    for col, title in enumerate(table_headers, start=1):
        ws.cell(row=8, column=col, value=title)

    # Fill token rows
    row = 9
    for rec in tokens.values():
        ws.cell(row=row, column=1, value=rec['symbol'])
        ws.cell(row=row, column=2, value=f"{rec['spent_sol']:.4f} SOL")
        ws.cell(row=row, column=3, value=f"{rec['earned_sol']:.4f} SOL")
        ws.cell(row=row, column=4, value=f"{rec['delta_sol']:.4f}")
        ws.cell(row=row, column=5, value=f"{rec['delta_pct']:.2f}%")
        ws.cell(row=row, column=6, value=rec['buys'])
        ws.cell(row=row, column=7, value=rec['sells'])
        if rec['last_trade']:
            ws.cell(row=row, column=8, value=rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(row=row, column=9, value=rec.get('in_tokens', 0))
        ws.cell(row=row, column=10, value=rec.get('out_tokens', 0))
        ws.cell(row=row, column=11, value=f"{rec.get('fee', 0):.4f}")
        ws.cell(row=row, column=12, value=rec.get('period', '-'))
        ws.cell(row=row, column=13, value=rec.get('first_mcap', ''))
        ws.cell(row=row, column=14, value=rec.get('last_mcap', ''))
        ws.cell(row=row, column=15, value=rec.get('current_mcap', ''))
        ws.cell(row=row, column=16, value=rec['mint'])
        # Hyperlinks
        cell_dex = ws.cell(row=row, column=17)
        cell_dex.value = 'View trades'
        cell_dex.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        cell_ph = ws.cell(row=row, column=18)
        cell_ph.value = 'View trades'
        cell_ph.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        row += 1

    wb.save(filename)
    return filename
