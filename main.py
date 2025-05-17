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

# Analyze wallet trades using Dexscreener trades endpoint
def analyze_wallet(wallet):
    # Get native balance
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get("nativeBalance", 0) / 1e9

    # Fetch recent transactions to gather token mints
    txs = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    ) or []
    mints = {tr["mint"] for tx in txs for tr in tx.get("tokenTransfers", [])
             if tr.get("mint") and (tr.get("toUserAccount") == wallet or tr.get("fromUserAccount") == wallet)}

    tokens = {}
    # For each mint, fetch trades from Dexscreener
    for mint in mints:
        rec = {
            "mint": mint,
            "symbol": get_symbol(mint),
            "spent_sol": 0,
            "earned_sol": 0,
            "buys": 0,
            "sells": 0,
            "in_tokens": 0,
            "out_tokens": 0,
            "fee": 0,
            "first_ts": None,
            "last_ts": None,
            "first_mcap": "",
            "last_mcap": "",
            "current_mcap": ""
        }
        data = safe_request(f"{DEXSCREENER_BASE}trades/solana/{mint}?maker={wallet}")
        for t in data.get("trades", []):
            side = t.get("side")  # "buy" or "sell"
            ts = datetime.fromtimestamp(t.get("timestamp", 0) / 1000)
            amt_token = float(t.get("amount", 0))
            amt_sol = float(t.get("amountQuote", 0)) / 1e9
            if side == "buy":
                rec["buys"] += 1
                rec["spent_sol"] += amt_sol
                rec["in_tokens"] += amt_token
                if rec["first_ts"] is None or ts < rec["first_ts"]:
                    rec["first_ts"] = ts
            else:
                rec["sells"] += 1
                rec["earned_sol"] += amt_sol
                rec["out_tokens"] += amt_token
                if rec["last_ts"] is None or ts > rec["last_ts"]:
                    rec["last_ts"] = ts
        # Compute PnL, percentages, durations
        rec["delta_sol"] = rec["earned_sol"] - rec["spent_sol"]
        rec["delta_pct"] = (rec["delta_sol"] / rec["spent_sol"] * 100) if rec["spent_sol"] else 0
        rec["period"] = format_duration(rec["first_ts"], rec["last_ts"])
        rec["last_trade"] = rec["last_ts"] or rec["first_ts"]
        tokens[mint] = rec

    summary = {
        "wallet": wallet,
        "balance": balance,
        "pnl": sum(r["delta_sol"] for r in tokens.values()),
        "avg_win_pct": sum(r["delta_pct"] for r in tokens.values() if r["delta_sol"] > 0)
                        / max(1, sum(1 for r in tokens.values() if r["delta_sol"] > 0)),
        "pnl_loss": sum(r["delta_sol"] for r in tokens.values() if r["delta_sol"] < 0),
        "balance_change": sum(r["delta_sol"] for r in tokens.values())
                           / max(1, balance) * 100,
        "winrate": sum(1 for r in tokens.values() if r["delta_sol"] > 0)
                    / max(1, len(tokens)) * 100,
        "time_period": "30 days",
        "sol_price": SOL_PRICE
    }
    return tokens, summary

# Generate Excel report
def generate_excel(wallet, tokens, summary):
    filename = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Report"

    # Summary row
    headers = ["Wallet", "WinRate", "PnL", "Avg Win %", "PnL Loss", "Balance change %", "Period", "SOL Price", "Balance"]
    ws.append(headers)
    ws.append([
        wallet,
        f"{summary['winrate']:.2f}%",
        f"{summary['pnl']:.4f}",
        f"{summary['avg_win_pct']:.2f}%",
        f"{summary['pnl_loss']:.4f}",
        f"{summary['balance_change']:.2f}%",
        summary['time_period'],
        summary['sol_price'],
        f"{summary['balance']:.4f}"
    ])

    # Token details header
    ws.append([])
    detail_headers = [
        "Token", "Spent SOL", "Earned SOL", "Delta SOL", "%", "Buys", "Sells", "In", "Out", "Period", "Last trade"
    ]
    ws.append(detail_headers)

    for rec in tokens.values():
        ws.append([
            rec['symbol'],
            f"{rec['spent_sol']:.4f}",
            f"{rec['earned_sol']:.4f}",
            f"{rec['delta_sol']:.4f}",
            f"{rec['delta_pct']:.2f}%",
            rec['buys'],
            rec['sells'],
            rec['in_tokens'],
            rec['out_tokens'],
            rec['period'],
            rec['last_trade'].strftime('%d.%m.%Y') if rec['last_trade'] else ''
        ])

    wb.save(filename)
    return filename

# Flask routes
@app.route('/', methods=['GET'])
def health_check():
    return 'OK', 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def webhook():
    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return 'OK', 200

# Telegram handlers
@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.reply_to(message, "Привет! Отправь мне Solana-адрес, и я пришлю отчёт.")

@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    bot.reply_to(message, "Обрабатываю...")
    tokens, summary = analyze_wallet(wallet)
    report_file = generate_excel(wallet, tokens, summary)
    with open(report_file, 'rb') as f:
        bot.send_document(message.chat.id, f)

# Run Flask server for webhook
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
