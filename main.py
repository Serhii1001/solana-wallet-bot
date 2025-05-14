import os
import requests
import telebot
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

# ========== Конфиг ==========
TELEGRAM_TOKEN   = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ========== HTTP helpers ==========
def safe_request(url, params=None):
    """Надёжный GET с тремя попытками."""
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {}

# ========== Метаданные токена ==========
def get_symbol(mint):
    """
    Берём символ токена через Dexscreener (там гарантированно есть stats.symbol).
    Если сломается, вернём сам mint.
    """
    data = safe_request(DEXSCREENER_BASE + mint)
    return data.get("stats", {}).get("symbol", mint)

# ========== MCAP ==========
def get_historical_mcap(mint, ts_dt):
    """
    Историческая MCAP близко к ts_dt (интервал 1h).
    """
    data = safe_request(f"{DEXSCREENER_BASE}{mint}/chart?interval=1h")
    points = data.get("chart", [])
    if not points:
        return ""
    target = int(ts_dt.timestamp() * 1000)
    best = min(points, key=lambda p: abs(p.get("timestamp", 0) - target))
    return best.get("marketCap", "")

def get_current_mcap(mint):
    """
    Текущая MCAP из stats.marketCap.
    """
    data = safe_request(DEXSCREENER_BASE + mint)
    return data.get("stats", {}).get("marketCap", "")

# ========== Утилита форматирования длительности ==========
def format_duration(start, end):
    if not start or not end:
        return "-"
    delta = end - start
    total_sec = int(delta.total_seconds())
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{sec}s"

# ========== Анализ кошелька ==========
def analyze_wallet(wallet):
    # 1) Получаем транзакции
    txs = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
        params={"api-key": HELIUS_API_KEY, "limit": 100}
    ) or []

    # 2) Словарь для собираемых данных
    tokens = {}

    # 3) Баланс SOL
    bal = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/balances",
        params={"api-key": HELIUS_API_KEY}
    )
    balance = bal.get("nativeBalance", 0) / 1e9

    # 4) Перебираем все tx
    for tx in txs:
        ts   = datetime.fromtimestamp(tx.get("timestamp", 0))
        fee  = tx.get("fee", 0) / 1e9
        native = tx.get("nativeTransfers", [])

        # Складываем SOL-выход и вход
        sol_out = sum(n.get("amount", 0)
                      for n in native if n.get("fromUserAccount") == wallet) / 1e9
        sol_in  = sum(n.get("amount", 0)
                      for n in native if n.get("toUserAccount")   == wallet) / 1e9

        # Пробегаем tokenTransfers
        for tr in tx.get("tokenTransfers", []):
            mint     = tr.get("mint")
            amount   = float(tr.get("tokenAmount", 0)) / (10**tr.get("decimals", 0))
            # направление сделки
            direction = (
                "buy"  if tr.get("toUserAccount")   == wallet else
                "sell" if tr.get("fromUserAccount") == wallet else
                None
            )
            if not direction:
                continue

            # Инициализация записи
            rec = tokens.setdefault(mint, {
                "mint": mint,
                "symbol": get_symbol(mint),
                "spent_sol": 0.0,
                "earned_sol": 0.0,
                "fee": 0.0,
                "buys": 0,
                "sells": 0,
                "in_tokens": 0.0,
                "out_tokens": 0.0,
                "first_ts": None,
                "last_ts": None,
                "first_mcap": "",
                "last_mcap": "",
                "current_mcap": ""
            })

            # накапливаем комиссию
            rec["fee"] += fee

            if direction == "buy":
                rec["buys"]      += 1
                rec["in_tokens"] += amount
                rec["spent_sol"] += sol_out
                if not rec["first_ts"]:
                    rec["first_ts"]   = ts
                    rec["first_mcap"] = get_historical_mcap(mint, ts)
            else:
                rec["sells"]      += 1
                rec["out_tokens"] += amount
                rec["earned_sol"] += sol_in
                rec["last_ts"]     = ts
                rec["last_mcap"]   = get_historical_mcap(mint, ts)

    # 5) Финализация метрик
    wins = losses = 0
    total_win_pct = 0.0
    for rec in tokens.values():
        rec["delta_sol"] = rec["earned_sol"] - rec["spent_sol"]
        rec["delta_pct"] = (rec["delta_sol"] / rec["spent_sol"] * 100
                            if rec["spent_sol"] else 0)
        rec["period"]     = format_duration(rec["first_ts"], rec["last_ts"])
        rec["last_trade"] = rec["last_ts"] or rec["first_ts"]
        rec["current_mcap"] = get_current_mcap(rec["mint"])
        if rec["delta_sol"] > 0:
            wins += 1
            total_win_pct += rec["delta_pct"]
        elif rec["delta_sol"] < 0:
            losses += 1

    winrate = round(wins / (wins + losses) * 100, 2) if (wins + losses) else 0
    avg_win  = round(total_win_pct / wins, 2) if wins else 0
    pnl      = round(sum(r["delta_sol"] for r in tokens.values()), 2)
    pnl_loss = round(sum(r["delta_sol"] for r in tokens.values() if r["delta_sol"]<0), 2)
    bal_change = round(pnl / (balance - pnl) * 100, 2) if balance else 0

    summary = {
        "wallet": wallet,
        "balance": balance,
        "pnl": pnl,
        "avg_win_pct": avg_win,
        "pnl_loss": pnl_loss,
        "balance_change": bal_change,
        "winrate": winrate,
        "time_period": "30 days",
        "sol_price": SOL_PRICE
    }
    return tokens, summary

# ========== Генерация Excel ==========
def generate_excel(wallet, tokens, summary):
    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Report"

    # Метаданные
    meta = [
        ("Wallet", wallet),
        ("WinRate", f"{summary['winrate']}%"),
        ("PnL R", f"{summary['pnl']:.2f} SOL"),
        ("Avg Win %", f"{summary['avg_win_pct']:.2f}%"),
        ("PnL Loss", f"{summary['pnl_loss']:.2f} SOL"),
        ("Balance change", f"{summary['balance_change']:.2f}%"),
        ("TimePeriod", summary["time_period"]),
        ("SOL Price Now", f"{summary['sol_price']} $"),
        ("Balance", f"{summary['balance']:.2f} SOL"),
    ]
    for i, (k, v) in enumerate(meta, start=1):
        ws[f"A{i}"] = k
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"B{i}"] = v

    # Строка MCAP-категорий
    start = len(meta) + 2
    ws[f"A{start}"] = "Tokens entry MCAP:"
    ws[f"A{start}"].font = Font(bold=True)
    for idx, cat in enumerate(["<5k","5k–30k","30k–100k","100k–300k","300k+"], start=1):
        ws.cell(row=start, column=idx+1, value=cat)

    # Заголовки таблицы
    headers = [
        "Token","Spent SOL","Earned SOL","Delta Sol","Delta %",
        "Buys","Sells","Last trade","Income","Outcome",
        "Fee","Period","First buy Mcap","Last tx Mcap","Current Mcap",
        "Contract","Dexscreener","Photon"
    ]
    ws.append(headers)
    for cell in ws[start+2]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDDDDD")

    # Данные по токенам
    for rec in tokens.values():
        row = [
            rec["symbol"],
            f"{rec['spent_sol']:.2f} SOL",
            f"{rec['earned_sol']:.2f} SOL",
            f"{rec['delta_sol']:.2f} SOL",
            f"{rec['delta_pct']:.2f}%",
            rec["buys"],
            rec["sells"],
            rec["last_trade"].strftime("%d.%m.%Y"),
            f"{rec['in_tokens']:.6f}",
            f"{rec['out_tokens']:.6f}",
            f"{rec['fee']:.5f}",
            rec["period"],
            rec.get("first_mcap",""),
            rec.get("last_mcap",""),
            rec.get("current_mcap",""),
            rec["mint"],
            f'=HYPERLINK("https://dexscreener.com/solana/{rec["mint"]}?maker={wallet}", "View trades")',
            f'=HYPERLINK("https://photon.tools/token/{rec["mint"]}",        "View trades")'
        ]
        ws.append(row)
        cr = ws.max_row
        d_cell = ws[f"D{cr}"]; p_cell = ws[f"E{cr}"]
        fill = PatternFill("solid", fgColor="C6EFCE") if rec["delta_sol"]>0 else PatternFill("solid", fgColor="FFC7CE")
        d_cell.fill = p_cell.fill = fill

    # Сохранение
    fname = f"{wallet}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(fname)
    return fname

# ========== Хендлеры бота ==========
@bot.message_handler(commands=['start'])
def cmd_start(msg):
    bot.reply_to(msg, "Привет! Пришли Solana-адрес.")

@bot.message_handler(func=lambda m: True)
def cmd_wallet(msg):
    wallet = msg.text.strip()
    bot.reply_to(msg, "Собираю отчет, подожди...")
    tokens, summary = analyze_wallet(wallet)
    fname = generate_excel(wallet, tokens, summary)
    with open(fname, "rb") as f:
        bot.send_document(msg.chat.id, f)

# ========== Запуск ==========
if __name__ == "__main__":
    bot.remove_webhook()
    bot.infinity_polling()
