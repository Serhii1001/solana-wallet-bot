import os
import json
import requests
import telebot
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

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
    best = min(points, key=lambda p: abs(p.get('timestamp',0) - target))
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
    # Fetch transactions
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    txs = safe_request(url) or []
    tokens = {}
    # Fetch balance
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9

    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp',0))
        fee = tx.get('fee',0) / 1e9
        # map tokenTransfers
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount',0)) / (10**tr.get('decimals',0))
            direction = ("buy" if tr.get('toUserAccount')==wallet else
                         "sell" if tr.get('fromUserAccount')==wallet else None)
            if not direction:
                continue
            rec = tokens.setdefault(mint, {
                "mint": mint,
                "symbol": get_symbol(mint),
                "spent_sol":0,"earned_sol":0,
                "delta_sol":0,"delta_pct":0,
                "buys":0,"sells":0,
                "in_tokens":0,"out_tokens":0,"fee":0,
                "first_ts":None,"last_ts":None,
                "first_mcap":"","last_mcap":"",
                "current_mcap":""
            })
            # accumulate counts
            if direction=="buy":
                rec["buys"]+=1
                rec["in_tokens"]+=amount
                rec["spent_sol"]+= fee  # approximate: use fee as spent
                if not rec["first_ts"]:
                    rec["first_ts"]=ts
                    rec["first_mcap"]=get_historical_mcap(mint,ts)
            else:
                rec["sells"]+=1
                rec["out_tokens"]+=amount
                rec["earned_sol"]+= fee  # approximate
                rec["last_ts"]=ts
                rec["last_mcap"]=get_historical_mcap(mint,ts)
            rec["fee"]+=fee

    # finalize metrics
    for rec in tokens.values():
        rec["delta_sol"]=rec["earned_sol"]-rec["spent_sol"]
        rec["delta_pct"]=(rec["delta_sol"]/rec["spent_sol"]*100
                          if rec["spent_sol"] else 0)
        rec["period"]=format_duration(rec["first_ts"],rec["last_ts"])
        rec["last_trade"]=rec["last_ts"] or rec["first_ts"]
        rec["current_mcap"]=get_current_mcap(rec["mint"])

    summary = {
        "wallet": wallet,
        "balance": balance,
        "pnl": sum(r["delta_sol"] for r in tokens.values()),
        "avg_win_pct": (sum(r["delta_pct"] for r in tokens.values() if r["delta_sol"]>0)/
                        max(1,sum(1 for r in tokens.values() if r["delta_sol"]>0))),
        "pnl_loss": sum(r["delta_sol"] for r in tokens.values() if r["delta_sol"]<0),
        "balance_change": (sum(r["delta_sol"] for r in tokens.values())/
                           (balance - sum(r["delta_sol"] for r in tokens.values()))*100
                          if balance else 0),
        "winrate": (sum(1 for r in tokens.values() if r["delta_sol"]>0)/
                    max(1,sum(1 for r in tokens.values() if abs(r["delta_sol"])>0))*100),
        "time_period":"30 days",
        "sol_price": SOL_PRICE
    }
    return tokens, summary

def generate_excel(wallet, tokens, summary):
    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Report"
    # [insert the updated generate_excel code here]
    # ...
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
