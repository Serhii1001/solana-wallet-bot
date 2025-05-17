import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime, timedelta
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
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

# Fetch and filter transactions via Helius
def get_transactions(wallet, since_days):
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=1000"
    data = safe_request(url)
    txs = data if isinstance(data, list) else data.get("transactions", [])
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    filtered = []
    for tx in txs:
        ts = tx.get("timestamp")
        if not ts:
            continue
        tdt = datetime.utcfromtimestamp(ts)
        if tdt >= cutoff:
            filtered.append(tx)
    print(f"[DEBUG] Found {len(filtered)} transactions for last {since_days} days")
    return filtered

# Fetch token symbol
def get_symbol(mint):
    data = safe_request(f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}")
    return data.get("symbol", mint)

# Historical market cap
def get_historical_mcap(mint, ts):
    chart = safe_request(f"{DEXSCREENER_BASE}tokens/solana/{mint}/chart?interval=1h").get('chart', [])
    if not chart:
        return ''
    target = int(ts.timestamp() * 1000)
    best = min(chart, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', '')

# Current market cap
def get_current_mcap(mint):
    return safe_request(f"{DEXSCREENER_BASE}tokens/solana/{mint}").get('stats', {}).get('marketCap', '')

# Format duration between two datetimes
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

# Analyze wallet trades
def analyze_wallet(wallet, since_days):
    txs = get_transactions(wallet, since_days)
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9

    tokens = {}
    # initial pass to collect all mints
    for tx in txs:
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            if mint:
                tokens.setdefault(mint, {
                    'mint': mint,
                    'symbol': get_symbol(mint),
                    'buys': 0, 'sells': 0,
                    'spent_sol': 0, 'earned_sol': 0,
                    'in_tokens': 0, 'out_tokens': 0,
                    'fee': 0,
                    'first_ts': None, 'last_ts': None,
                    'first_mcap': '', 'last_mcap': '', 'current_mcap': ''
                })
    # override with Dexscreener
    for rec in tokens.values():
        data = safe_request(f"{DEXSCREENER_BASE}trades/solana/{rec['mint']}?maker={wallet}")
        trades = data.get('trades', [])
        # count and sum
        rec['buys'] = sum(1 for t in trades if t.get('side')=='buy')
        rec['sells'] = sum(1 for t in trades if t.get('side')=='sell')
        rec['spent_sol'] = sum(float(t.get('amountQuote',0))/1e9 for t in trades if t.get('side')=='buy')
        rec['earned_sol'] = sum(float(t.get('amountQuote',0))/1e9 for t in trades if t.get('side')=='sell')
        rec['in_tokens'] = sum(float(t.get('amount',0)) for t in trades if t.get('side')=='buy')
        rec['out_tokens'] = sum(float(t.get('amount',0)) for t in trades if t.get('side')=='sell')
        buy_times = [datetime.fromtimestamp(t.get('timestamp',0)/1000) for t in trades if t.get('side')=='buy']
        sell_times = [datetime.fromtimestamp(t.get('timestamp',0)/1000) for t in trades if t.get('side')=='sell']
        if buy_times:
            first = min(buy_times)
            rec['first_ts'] = first
            rec['first_mcap'] = get_historical_mcap(rec['mint'], first)
        if sell_times:
            last = max(sell_times)
            rec['last_ts'] = last
            rec['last_mcap'] = get_historical_mcap(rec['mint'], last)
        # final metrics
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol']/rec['spent_sol']*100) if rec['spent_sol'] else 0
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    # summary
    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': sum(r['delta_pct'] for r in tokens.values() if r['delta_sol']>0)
                       / max(1,sum(1 for r in tokens.values() if r['delta_sol']>0)),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol']<0),
        'balance_change': sum(r['delta_sol'] for r in tokens.values())/max(1,balance)*100,
        'winrate': sum(1 for r in tokens.values() if r['delta_sol']>0)/max(1,len(tokens))*100,
        'time_period': f"{since_days} days",
        'sol_price': SOL_PRICE
    }
    return tokens, summary

# Generate Excel report
def generate_excel(wallet, tokens, summary):
    filename = f"{wallet}_report.xlsx"
    wb = Workbook(); ws = wb.active; ws.title = "Wallet Report"
    hdr = ['Token','Buys','Sells','Spent SOL','Earned SOL','Delta','%','First','Last','Fee']
    ws.append(hdr)
    for rec in tokens.values():
        ws.append([
            rec['symbol'], rec['buys'], rec['sells'],
            f"{rec['spent_sol']:.4f}", f"{rec['earned_sol']:.4f}", f"{rec['delta_sol']:.4f}",
            f"{rec['delta_pct']:.2f}%",
            rec['first_ts'].strftime('%d.%m.%Y') if rec['first_ts'] else '',
            rec['last_ts'].strftime('%d.%m.%Y') if rec['last_ts'] else '',
            f"{rec['fee']:.4f}"
        ])
    wb.save(filename)
    return filename

# Handlers
analyze_days = {}

@app.route('/', methods=['GET'])
def health():
    return 'OK', 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def hook():
    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return 'OK', 200

@bot.message_handler(commands=['analyze'])
def analyze_cmd(message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /analyze <days> [wallet]")
        return
    try:
        days = int(parts[1])
    except ValueError:
        bot.reply_to(message, "Первый аргумент должен быть числом дней.")
        return
    # If wallet provided in same command
    if len(parts) >= 3:
        wallet = parts[2]
        bot.reply_to(message, f"Анализ {days} дней для {wallet}...")
        tokens, summary = analyze_wallet(wallet, days)
        if not tokens:
            bot.reply_to(message, "Не найдено транзакций за указанный период.")
            return
        report = generate_excel(wallet, tokens, summary)
        with open(report, 'rb') as f:
            bot.send_document(message.chat.id, f)
    else:
        analyze_days[message.chat.id] = days
        bot.reply_to(message, f"Период анализа установлен: {days} дней. Теперь пришлите адрес кошелька.")

@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    # Ensure valid Solana address
    if len(wallet) not in [32, 44]:
        bot.reply_to(message, "Отправьте корректный адрес Solana-кошелька.")
        return
    days = analyze_days.get(message.chat.id, 30)
    bot.reply_to(message, f"Анализ {days} дней для {wallet}...")
    tokens, summary = analyze_wallet(wallet, days)
    if not tokens:
        bot.reply_to(message, "Не найдено транзакций за указанный период.")
        return
    report = generate_excel(wallet, tokens, summary)
    with open(report, 'rb') as f:
        bot.send_document(message.chat.id, f)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
