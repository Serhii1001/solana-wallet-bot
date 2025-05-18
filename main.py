import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook
from bs4 import BeautifulSoup

# Configuration
TELEGRAM_TOKEN   = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
PHOTON_BASE      = "https://photon-sol.tinyastro.io/en/lp/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

# Initialize bot and app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Configure webhook
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

# DEX spend helpers
def get_spent_via_dexscreener(mint, maker):
    """Fetches and sums SOL spent on trades for a given mint and maker from Dexscreener"""
    url = f"{DEXSCREENER_BASE}{mint}/trades?maker={maker}"
    data = safe_request(url) or {}
    total = 0.0
    for t in data.get('trades', []):
        if t.get('baseToken') == 'SOL':
            total += float(t.get('baseAmount', 0))
    return total


def get_spent_via_photon(mint, maker):
    """Parses Photon LP page and sums SOL spent by maker on buys"""
    url = f"{PHOTON_BASE}{mint}"
    try:
        html = requests.get(url, timeout=10).text
        soup = BeautifulSoup(html, 'html.parser')
        total = 0.0
        for row in soup.select('table tr'):
            cols = [c.get_text(strip=True) for c in row.find_all('td')]
            if len(cols) >= 4:
                maker_addr, token_in, amount_in = cols[1], cols[2], cols[3]
                if maker_addr.lower() == maker.lower() and 'SOL' in token_in:
                    total += float(amount_in.replace(',', ''))
        return total
    except Exception:
        return 0.0

# Helper functions for market caps and durations
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
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/enhanced-transactions?api-key={HELIUS_API_KEY}&limit=1000"
    txs = safe_request(url)
    
    tokens = {}
    balance_data = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = balance_data.get("nativeBalance", 0) / 1e9

    for tx in txs:
        ts = datetime.fromtimestamp(tx.get("timestamp", 0))
        events = tx.get("events", {})
        instructions = tx.get("tokenTransfers", [])
        fee = tx.get("fee", 0) / 1e9

        for swap in events.get("swaps", []):
            base = swap.get("source")
            quote = swap.get("destination")
            amount_in = float(swap.get("nativeInputAmount", 0)) / 1e9
            amount_out = float(swap.get("nativeOutputAmount", 0)) / 1e9

            base_mint = swap.get("sourceMint")
            quote_mint = swap.get("destinationMint")

            # Determine direction
            if swap.get("sourceUserAccount") == wallet:
                direction = "buy"
                mint = quote_mint
                token_amt = swap.get("tokenAmount", 0)
                sol_spent = amount_in
                sol_earned = 0
            elif swap.get("destinationUserAccount") == wallet:
                direction = "sell"
                mint = base_mint
                token_amt = swap.get("tokenAmount", 0)
                sol_spent = 0
                sol_earned = amount_out
            else:
                continue  # skip irrelevant swap

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

            if direction == "buy":
                rec["buys"] += 1
                rec["spent_sol"] += sol_spent
                rec["in_tokens"] += token_amt
                if not rec["first_ts"]:
                    rec["first_ts"] = ts
                    rec["first_mcap"] = get_historical_mcap(mint, ts)
            else:
                rec["sells"] += 1
                rec["earned_sol"] += sol_earned
                rec["out_tokens"] += token_amt
                rec["last_ts"] = ts
                rec["last_mcap"] = get_historical_mcap(mint, ts)

            rec["fee"] += fee

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
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }

    return tokens, summary


# Excel report
def generate_excel(wallet, tokens, summary):
    fn = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"
    hdr = [
        'Wallet','WinRate','PnL R','Avg Win %','PnL Loss',
        'Balance change','TimePeriod','SOL Price Now','Balance'
    ]
    for i,t in enumerate(hdr,1): ws.cell(1,i,t)
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
    for i,v in enumerate(vals,1): ws.cell(2,i,v)
    ws.cell(4,1,'Tokens entry MCAP:')
    ranges = ['<5k','5k-30k','30k-100k','100k-300k','300k+']
    for i,r in enumerate(ranges,2): ws.cell(5,i,r)
    cols = [
        'Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys',
        'Sells','Last trade','Income','Outcome','Fee','Period',
        'First buy Mcap','Last tx Mcap','Current Mcap','Contract',
        'Dexscreener','Photon'
    ]
    for i,c in enumerate(cols,1): ws.cell(8,i,c)
    r = 9
    for rec in tokens.values():
        ws.cell(r,1,rec['symbol'])
        ws.cell(r,2,f"{rec['spent_sol']:.2f} SOL")
        ws.cell(r,3,f"{rec['earned_sol']:.2f} SOL")
        ws.cell(r,4,f"{rec['delta_sol']:.2f}")
        ws.cell(r,5,f"{rec['delta_pct']:.2f}%")
        ws.cell(r,6,rec['buys'])
        ws.cell(r,7,rec['sells'])
        if rec['last_trade']: ws.cell(r,8,rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(r,9,rec['in_tokens'])
        ws.cell(r,10,rec['out_tokens'])
        ws.cell(r,11,f"{rec['fee']:.2f}")
        ws.cell(r,12,rec['period'])
        ws.cell(r,13,rec['first_mcap'])
        ws.cell(r,14,rec['last_mcap'])
        ws.cell(r,15,rec['current_mcap'])
        ws.cell(r,16,rec['mint'])
        d = ws.cell(r,17)
        d.value = 'View trades'
        d.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        p = ws.cell(r,18)
        p.value = 'View trades'
        p.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        r += 1
    wb.save(fn)
    return fn

# Handlers
def welcome(m): bot.reply_to(m,"Привет! Отправь Solana-адрес.")
bot.register_message_handler(welcome, commands=['start'])

def handle(m):
    wallet = m.text.strip()
    bot.reply_to(m,"Обрабатываю...")
    tokens, summary = analyze_wallet(wallet)
    f = generate_excel(wallet, tokens, summary)
    bot.send_document(m.chat.id, open(f, 'rb'))
bot.register_message_handler(handle, func=lambda _: True)

# Test specific mint spend
if __name__ == '__main__':
    # Debug: check spend for specific mint and wallet
    test_mint = "6aaf35zf8byn3bdc2kvcneaygiqjjrdhbtdptn7y6gr"
    test_wallet = "7ajzTPBLuHDEe9pX3txVn32UnFQaEuZsUe9BkBzg1s4P"
    spend = get_spent_via_dexscreener(test_mint, test_wallet)
    print(f"Spent SOL for mint {test_mint} by wallet {test_wallet}: {spend:.4f} SOL")
    # Run the app server
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
