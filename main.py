
import os
import json
import requests
import telebot
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from datetime import datetime
import threading
import http.server
import socketserver

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
SOL_PRICE = 239

bot = telebot.TeleBot(TELEGRAM_TOKEN)

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

def get_transactions(wallet):
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    response = requests.get(url)
    with open("debug_helius.txt", "w", encoding="utf-8") as f:
        f.write(response.text)
    if response.status_code != 200:
        return []
    return response.json()

def analyze_wallet(wallet):
    txs = get_transactions(wallet)
    tokens = {}
    total_profit = 0
    total_loss = 0
    wins = 0
    losses = 0

    for tx in txs:
        timestamp = tx.get("timestamp")
        transfers = tx.get("tokenTransfers", [])
        for tr in transfers:
            mint = tr.get("mint")
            amount = float(tr.get("tokenAmount", 0)) / 10**tr.get("decimals", 0)
            from_acc = tr.get("fromUserAccount")
            to_acc = tr.get("toUserAccount")
            direction = "buy" if to_acc == wallet else "sell" if from_acc == wallet else "other"
            if direction == "other":
                continue

            if mint not in tokens:
                tokens[mint] = {
                    "mint": mint,
                    "buy_count": 0,
                    "sell_count": 0,
                    "buy_amount": 0,
                    "sell_amount": 0,
                    "buy_ts": None,
                    "sell_ts": None,
                    "profit": 0,
                    "duration": "-"
                }

            t = tokens[mint]
            if direction == "buy":
                t["buy_count"] += 1
                t["buy_amount"] += amount
                if not t["buy_ts"]:
                    t["buy_ts"] = timestamp
            else:
                t["sell_count"] += 1
                t["sell_amount"] += amount
                if not t["sell_ts"]:
                    t["sell_ts"] = timestamp
                t["profit"] = t["sell_amount"] - t["buy_amount"]
                if t["buy_ts"] and t["sell_ts"]:
                    t["duration"] = format_duration(t["buy_ts"], t["sell_ts"])

    for t in tokens.values():
        if t["profit"] > 0:
            total_profit += t["profit"]
            wins += 1
        elif t["profit"] < 0:
            total_loss += abs(t["profit"])
            losses += 1

    winrate = round(100 * wins / (wins + losses), 2) if wins + losses > 0 else 0
    balance = sum([t["sell_amount"] for t in tokens.values()])

    return tokens, {
        "wallet": wallet,
        "balance": round(balance, 2),
        "pnl": round(total_profit - total_loss, 2),
        "winrate": winrate,
        "pnl_profit": round(total_profit, 2),
        "pnl_loss": round(-total_loss, 2),
        "sol_price": SOL_PRICE,
        "time_period": "30 days"
    }

def generate_excel(wallet, tokens, summary):
    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Report"

    meta = [
        ("Wallet", summary["wallet"]),
        ("WinRate", f"{summary['winrate']}%"),
        ("PnL (SOL)", summary["pnl"]),
        ("PnL Profit (SOL)", summary["pnl_profit"]),
        ("PnL Loss (SOL)", summary["pnl_loss"]),
        ("Balance (SOL)", summary["balance"]),
        ("SOL Price Now", summary["sol_price"]),
        ("TimePeriod", summary["time_period"]),
    ]

    for i, (k, v) in enumerate(meta, start=1):
        ws[f"A{i}"] = k
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"B{i}"] = v

    start_row = len(meta) + 2
    headers = [
        "Token", "Buy Count", "Sell Count", "Buy Amount", "Sell Amount",
        "Profit (SOL)", "Profit (%)", "Time in Trade", "Solscan", "Birdeye"
    ]
    ws.append(headers)
    for col in ws.iter_cols(min_row=start_row, max_row=start_row, min_col=1, max_col=len(headers)):
        for cell in col:
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    for t in tokens.values():
        profit_pct = round((t["profit"] / t["buy_amount"] * 100), 2) if t["buy_amount"] else 0
        row = [
            t["mint"], t["buy_count"], t["sell_count"],
            round(t["buy_amount"], 4), round(t["sell_amount"], 4),
            round(t["profit"], 4), profit_pct, t["duration"],
            f"https://solscan.io/token/{t['mint']}",
            f"https://birdeye.so/token/{t['mint']}"
        ]
        ws.append(row)

        profit_cell = ws[f"F{ws.max_row}"]
        profit_pct_cell = ws[f"G{ws.max_row}"]
        if t["profit"] > 0:
            profit_cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
            profit_pct_cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        elif t["profit"] < 0:
            profit_cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
            profit_pct_cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

    filename = f"{wallet}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(filename)
    return filename

@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "Привет! Отправь мне адрес Solana-кошелька.")

@bot.message_handler(func=lambda message: True)
def process_wallet(message):
    wallet = message.text.strip()
    if len(wallet) in [32, 44] and wallet.isalnum():
        bot.reply_to(message, "Обрабатываю, подожди...")
        try:
            tokens, summary = analyze_wallet(wallet)
            if not tokens:
                bot.send_message(message.chat.id, "Не удалось найти токены или транзакции.")
                return
            excel = generate_excel(wallet, tokens, summary)
            with open(excel, "rb") as f:
                bot.send_document(message.chat.id, f)
            os.remove(excel)
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
    else:
        bot.reply_to(message, "Пожалуйста, отправь корректный Solana-адрес.")

bot.remove_webhook()
threading.Thread(target=bot.polling, daemon=True).start()
PORT = 10000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Слушаю порт {PORT}")
    httpd.serve_forever()
