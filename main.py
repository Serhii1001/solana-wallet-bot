import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL")
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

# Initialize bot and app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Remove any old webhook and set new one
bot.delete_webhook()
bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# Helper for HTTP requests
def safe_request(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
    return {}

# Data helpers
def get_symbol(mint):
    return safe_request(f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}").get('symbol', mint)

def get_historical_mcap(mint, ts):
    pts = safe_request(f"{DEXSCREENER_BASE}tokens/solana/{mint}/chart?interval=1h").get('chart', [])
    if not pts:
        return ''
    target = int(ts.timestamp() * 1000)
    best = min(pts, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', '')

def get_current_mcap(mint):
    return safe_request(f"{DEXSCREENER_BASE}tokens/solana/{mint}").get('stats', {}).get('marketCap', '')

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

# Core processing
def analyze_wallet(wallet):
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9
    txs = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100" ) or []
    mints = set(tr['mint'] for tx in txs for tr in tx.get('tokenTransfers', [])
                if tr.get('mint') and (tr.get('toUserAccount')==wallet or tr.get('fromUserAccount')==wallet))
    tokens = {}
    for mint in mints:
        wb.save(filename)
    return filename
                   buys=0, sells=0, in_tokens=0, out_tokens=0,
                   first_ts=None, last_ts=None, first_mcap='', last_mcap='', current_mcap='')
        trades = safe_request(f"{DEXSCREENER_BASE}trades/solana/{mint}?maker={wallet}").get('trades', [])
        for t in trades:
            side = t.get('side'); ts = datetime.fromtimestamp(t.get('timestamp',0)/1000)
            amt_tok = float(t.get('amount', 0)); amt_sol = float(t.get('amountQuote', 0)) / 1e9
            if side == 'buy':
                rec['buys'] += 1
                rec['spent_sol'] += amt_sol
                rec['in_tokens'] += amt_tok
                if rec['first_ts'] is None or ts < rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['earned_sol'] += amt_sol
                rec['out_tokens'] += amt_tok
                if rec['last_ts'] is None or ts > rec['last_ts']:
                    rec['last_ts'] = ts
                    rec['last_mcap'] = get_historical_mcap(mint, ts)
        rec['current_mcap'] = get_current_mcap(mint)
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol'] / rec['spent_sol'] * 100) if rec['spent_sol'] else 0
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        tokens[mint] = rec
    summary = dict(
        wallet=wallet, balance=balance,
        pnl=sum(r['delta_sol'] for r in tokens.values()),
        avg_win_pct=sum(r['delta_pct'] for r in tokens.values() if r['delta_sol']>0)
                    / max(1, sum(1 for r in tokens.values() if r['delta_sol']>0)),
        pnl_loss=sum(r['delta_sol'] for r in tokens.values() if r['delta_sol']<0),
        balance_change=sum(r['delta_sol'] for r in tokens.values())
                       / ((balance - sum(r['delta_sol'] for r in tokens.values())) or 1) * 100,
        winrate=sum(1 for r in tokens.values() if r['delta_sol']>0)
                / max(1, sum(1 for r in tokens.values() if abs(r['delta_sol'])>0)) * 100,
        time_period='30 days', sol_price=SOL_PRICE
    )
    return tokens, summary

# Excel report generator
def generate_excel(wallet, tokens, summary):
    filename = f"{wallet}_report.xlsx"
    wb = Workbook(); ws = wb.active; ws.title = "ArGhost table"
    headers = ['Wallet','WinRate','PnL R','Avg Win %','PnL Loss','Balance change','TimePeriod','SOL Price Now','Balance']
    for col, h in enumerate(headers, 1): ws.cell(row=1, column=col, value=h)
    vals = [wallet, f"{summary['winrate']:.2f}%", f"{summary['pnl']:.2f} SOL",
            f"{summary['avg_win_pct']:.2f}%", f"{summary['pnl_loss']:.2f} SOL",
            f"{summary['balance_change']:.2f}%", summary['time_period'],
            f"{summary['sol_price']} $", f"{summary['balance']:.2f} SOL"]
    for col, v in enumerate(vals, 1): ws.cell(row=2, column=col, value=v)
    ws.cell(row=4, column=1, value='Tokens entry MCAP:')
    for idx, r in enumerate(['<5k','5k-30k','30k-100k','100k-300k','300k+'], 2): ws.cell(row=5, column=idx, value=r)
    cols = ['Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells','Last trade','Income','Outcome','Fee','Period','First buy Mcap','Last tx Mcap','Current Mcap','Contract','Dexscreener','Photon']
    for col, c in enumerate(cols,1): ws.cell(row=8, column=col, value=c)
    row = 9
    for rec in tokens.values():
        # Place symbol in first column
        ws.cell(row=row, column=1, value=rec['symbol'])
        ws.cell(row, 2, value=f"{rec['spent_sol']:.2f} SOL")
        ws.cell(row, 3, value=f"{rec['earned_sol']:.2f} SOL")
        ws.cell(row, 4, value=f"{rec['delta_sol']:.2f}")
        ws.cell(row, 5, value=f"{rec['delta_pct']:.2f}%")
        ws.cell(row, 6, value=rec['buys'])
        ws.cell(row, 7, value=rec['sells'])
        if rec['last_trade']:
            ws.cell(row, 8, value=rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(row, 9, value=rec['in_tokens'])
        ws.cell(row, 10, value=rec['out_tokens'])
        ws.cell(row, 11, value=f"{rec['fee']:.2f}")
        ws.cell(row, 12, value=rec['period'])
        ws.cell(row, 13, value=rec['first_mcap'])
        ws.cell(row, 14, value=rec['last_mcap'])
        ws.cell(row, 15, value=rec['current_mcap'])
        ws.cell(row, 16, value=rec['mint'])
        d_cell = ws.cell(row, 17)
        d_cell.value = 'View trades'
        d_cell.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        p_cell = ws.cell(row, 18)
        p_cell.value = 'View trades'
        p_cell.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        row += 1
    wb.save(filename)
    return filename
    return filename

# Flask routes
@app.route('/', methods=['GET'])
def health():
    return 'OK', 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def webhook():
    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return 'OK', 200

# Telegram handlers
@bot.message_handler(commands=['start'])
def cmd_start(msg):
    bot.reply_to(msg, 'Привет! Отправь Solana-адрес.')

@bot.message_handler(func=lambda m: True)
def handle_wallet(msg):
    bot.reply_to(msg, 'Обрабатываю...')
    tokens, summary = analyze_wallet(msg.text.strip())
    fn = generate_excel(msg.text.strip(), tokens, summary)
    with open(fn, 'rb') as f:
        bot.send_document(msg.chat.id, f)

# Run Flask server
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
