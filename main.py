import os
import json
import requests
import telebot
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from datetime import datetime, timedelta
import threading
import http.server
import socketserver

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

analyze_days = {}

def get_sol_balance(wallet):
    url = "https://api.mainnet-beta.solana.com"
    headers = {"Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet]
    }
    response = requests.post(url, headers=headers, json=payload)
    lamports = response.json().get("result", {}).get("value", 0)
    return lamports / 1e9

def get_transactions(wallet, since_days):
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=1000"
    response = requests.get(url)
    if response.status_code != 200:
        return []
    data = response.json()
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    return [tx for tx in data if tx.get("timestamp") and datetime.utcfromtimestamp(tx["timestamp"]) >= cutoff]

def analyze_wallet(wallet, since_days):
    txs = get_transactions(wallet, since_days)
    tokens = {}
    for tx in txs:
        ts = tx.get("timestamp")
        events = tx.get("events", {})
        swap = events.get("swap", {})
        transfers = tx.get("tokenTransfers", [])
        fee = tx.get("fee", 0)

        for tr in transfers:
            mint = tr.get("mint")
            from_acc = tr.get("fromUserAccount")
            to_acc = tr.get("toUserAccount")
            direction = "buy" if to_acc == wallet else "sell" if from_acc == wallet else None
            if not direction:
                continue

            if mint not in tokens:
                tokens[mint] = {
                    "mint": mint,
                    "buy_count": 0,
                    "sell_count": 0,
                    "income": 0,
                    "outcome": 0,
                    "spent": 0,
                    "earned": 0,
                    "buy_ts": None,
                    "sell_ts": None,
                    "fee": 0
                }

            t = tokens[mint]
            amount = float(tr.get("tokenAmount", 0)) / 10**tr.get("decimals", 0)
            t["fee"] += float(fee) / 1e9 if fee else 0

            if direction == "buy":
                t["buy_count"] += 1
                t["income"] += amount
                t["spent"] += float(swap.get("nativeInputAmount", 0)) / 1e9
                if not t["buy_ts"]:
                    t["buy_ts"] = ts
            else:
                t["sell_count"] += 1
                t["outcome"] += amount
                t["earned"] += float(swap.get("nativeOutputAmount", 0)) / 1e9
                if not t["sell_ts"]:
                    t["sell_ts"] = ts

    winrate = 0
    win = 0
    loss = 0
    for t in tokens.values():
        if t["earned"] > t["spent"]:
            win += 1
        elif t["earned"] < t["spent"]:
            loss += 1
    winrate = round(100 * win / (win + loss), 2) if (win + loss) > 0 else 0

    pnl = round(sum(t["earned"] - t["spent"] for t in tokens.values()), 4)
    balance = get_sol_balance(wallet)

    return tokens, {
        "wallet": wallet,
        "balance": round(balance, 4),
        "pnl": pnl,
        "winrate": winrate,
        "time_period": f"{since_days} days"
    }

def format_duration(start_ts, end_ts):
    if not start_ts or not end_ts:
        return "-"
    delta = abs(end_ts - start_ts)
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    elif hours:
        return f"{hours}h {minutes}m"
    elif minutes:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def generate_excel(wallet, tokens, summary):
    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Report"

    meta = [
        ("Wallet", summary["wallet"]),
        ("Balance (SOL)", summary["balance"]),
        ("PNL R (SOL)", summary["pnl"]),
        ("WinRate", f"{summary['winrate']}%"),
        ("TimePeriod", summary["time_period"]),
    ]

    for i, (k, v) in enumerate(meta, start=1):
        ws[f"A{i}"] = k
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"B{i}"] = v

    start_row = len(meta) + 2
    headers = [
        "Token", "To Buy", "To Sell", "Income", "Outcome", "Delta Tokens",
        "Spent", "Earned", "Delta %", "Fee", "Trades Period",
        "Solscan", "Birdeye"
    ]
    ws.append(headers)
    for col in ws.iter_cols(min_row=start_row, max_row=start_row, min_col=1, max_col=len(headers)):
        for cell in col:
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    for t in tokens.values():
        delta = t["income"] - t["outcome"]
        delta_percent = round(((t["earned"] - t["spent"]) / t["spent"] * 100), 2) if t["spent"] else 0
        row = [
            t["mint"], t["buy_count"], t["sell_count"],
            round(t["income"], 4), round(t["outcome"], 4), round(delta, 4),
            round(t["spent"], 4), round(t["earned"], 4), delta_percent,
            round(t["fee"], 6), format_duration(t["buy_ts"], t["sell_ts"]),
            f"https://solscan.io/token/{t['mint']}", f"https://birdeye.so/token/{t['mint']}"
        ]
        ws.append(row)

    filename = f"{wallet}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(filename)
    return filename

@bot.message_handler(commands=['analyze'])
def set_analyze_days(message):
    try:
        days = int(message.text.split()[1])
        analyze_days[message.chat.id] = days
        bot.send_message(message.chat.id, f"Выбран период анализа: {days} дней. Теперь отправь адрес кошелька.")
    except:
        bot.send_message(message.chat.id, "Формат команды: /analyze 7 или /analyze 30")

@bot.message_handler(func=lambda message: True)
def handle_wallet(message):
    wallet = message.text.strip()
    if len(wallet) not in [32, 44]:
        bot.send_message(message.chat.id, "Отправь корректный адрес Solana-кошелька.")
        return

    since_days = analyze_days.get(message.chat.id, 30)
    bot.send_message(message.chat.id, f"Анализирую за последние {since_days} дней...")
    try:
        tokens, summary = analyze_wallet(wallet, since_days)
        if not tokens:
            bot.send_message(message.chat.id, "Не найдено подходящих транзакций.")
            return
        excel = generate_excel(wallet, tokens, summary)
        with open(excel, "rb") as f:
            bot.send_document(message.chat.id, f)
        os.remove(excel)
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}")

bot.remove_webhook()
threading.Thread(target=bot.polling, daemon=True).start()
PORT = 10000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Слушаю порт {PORT}")
    httpd.serve_forever()
