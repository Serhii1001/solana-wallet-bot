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

# === Configuration ===
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
SOL_PRICE = float(os.getenv("SOL_PRICE", 0))  # Цена SOL в USD для расчётов
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# === Вспомогательные функции ===

def safe_request(url, timeout=10, retries=3):
    """Запрос с таймаутом и повторами"""
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except requests.exceptions.RequestException:
            continue
    return None


def get_transactions(wallet, limit=500):
    """Получаем tokenTransfers и nativeTransfers с пагинацией"""
    all_txs = []
    after = None
    while True:
        url = (
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
            f"?api-key={HELIUS_API_KEY}&limit=100"
        )
        if after:
            url += f"&before={after}"
        data = safe_request(url)
        if not data or not isinstance(data, list):
            break
        all_txs.extend(data)
        if len(data) < 100 or len(all_txs) >= limit:
            break
        # Для пагинации берём подпись последнего
        after = data[-1].get("signature")
    return all_txs


def format_datetime(ts):
    """Форматируем Unix TS (секунды или мс)"""
    if ts > 1e12:
        ts = ts / 1000
    return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


def get_mcap(mint):
    """Получаем текущую MCAP токена из Dexscreener"""
    data = safe_request(DEXSCREENER_BASE + mint)
    if not data:
        return None
    # в ответе ожидаем data['stats']['marketCap']
    return data.get('stats', {}).get('marketCap')


def analyze_wallet(wallet):
    txs = get_transactions(wallet)
    tokens = {}
    total_spent = 0.0
    total_earned = 0.0
    wins = 0
    losses = 0
    profit_percents = []

    for tx in txs:
        ts = tx.get('timestamp')
        ts_str = format_datetime(ts)
        native = tx.get('nativeTransfers', [])
        sol_spent = sum(n.get('amount', 0)/1e9 for n in native if n.get('fromUserAccount') == wallet)
        sol_earned = sum(n.get('amount', 0)/1e9 for n in native if n.get('toUserAccount') == wallet)

        for tr in tx.get('tokenTransfers', []):
            mint = tr['mint']
            amount = tr.get('tokenAmount', 0) / 10**tr.get('decimals', 0)
            direction = 'buy' if tr.get('toUserAccount') == wallet else 'sell' if tr.get('fromUserAccount') == wallet else 'other'
            if direction == 'other':
                continue

            rec = tokens.setdefault(mint, {
                'mint': mint,
                'spent_sol': 0.0,
                'earned_sol': 0.0,
                'buys': 0,
                'sells': 0,
                'first_buy_ts': None,
                'last_sell_ts': None
            })
            if direction == 'buy':
                rec['buys'] += 1
                rec['spent_sol'] += sol_spent
                if not rec['first_buy_ts']:
                    rec['first_buy_ts'] = ts_str
            else:
                rec['sells'] += 1
                rec['earned_sol'] += sol_earned
                rec['last_sell_ts'] = ts_str

    # Обработка итогов
    for rec in tokens.values():
        spent = rec['spent_sol']
        earned = rec['earned_sol']
        delta = earned - spent
        pct = round(delta / spent * 100, 2) if spent else 0
        rec['delta_sol'] = round(delta, 4)
        rec['delta_pct'] = pct
        rec['last_trade'] = rec['last_sell_ts'] or rec['first_buy_ts']
        # MCAP
        rec['entry_mcap'] = get_mcap(rec['mint'])
        rec['exit_mcap'] = get_mcap(rec['mint'])
        rec['current_mcap'] = get_mcap(rec['mint'])

        total_spent += spent
        total_earned += earned
        if delta > 0:
            wins += 1
            profit_percents.append(pct)
        elif delta < 0:
            losses += 1

    winrate = round(100 * wins / (wins + losses), 2) if wins + losses else 0
    avg_win = round(sum(profit_percents) / len(profit_percents), 2) if profit_percents else 0
    pnl = round(total_earned - total_spent, 4)
    balance_change_pct = round((total_earned - total_spent) / total_spent * 100, 2) if total_spent else 0
    balance_sol = round(total_earned, 4)

    summary = {
        'wallet': wallet,
        'winrate': winrate,
        'pnl_r': pnl,
        'avg_win_pct': avg_win,
        'pnl_loss': round(total_spent - total_earned if total_spent > total_earned else 0, 4),
        'balance_change_pct': balance_change_pct,
        'time_period': '30 days',
        'sol_price': f"{SOL_PRICE} $",
        'balance_sol': f"{balance_sol} SOL"
    }
    return tokens, summary


def generate_excel(tokens, summary):
    wb = Workbook()
    ws = wb.active

    # --- Метаданные ---
    meta_headers = ['Wallet', None, None, None,
                    'WinRate', 'PnL R', None, 'Avg Win %', 'PnL Loss', 'Balance change',
                    None, 'TimePeriod', 'SOL Price Now', 'Balance']
    meta_values  = [summary['wallet'], None, None, None,
                    f"{summary['winrate']}%", f"{summary['pnl_r']} SOL", None,
                    f"{summary['avg_win_pct']}%", f"{-summary['pnl_loss']} SOL", f"{summary['balance_change_pct']} %",
                    None, summary['time_period'], summary['sol_price'], summary['balance_sol']]
    ws.append(meta_headers)
    ws.append(meta_values)

    # Пустые строки
    ws.append([None]*len(meta_headers))
    ws.append(['Tokens entry MCAP:'])
    ws.append(['<5k','5k-30k','30k-100k','100k-300k','300k+'])
    ws.append([None]*len(meta_headers))
    ws.append([None]*len(meta_headers))

    # --- Заголовок таблицы ---
    headers = [
        'Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells','Last trade',
        'Income','Outcome','Fee','Period','First buy Mcap','Last tx Mcap','Current Mcap',
        'Contract','Dexscreener','Photon'
    ]
    ws.append(headers)
    # Форматирование заголовка
    header_row = ws.max_row
    for col in range(1, len(headers)+1):
        cell = ws.cell(row=header_row, column=col)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    # --- Данные по токенам ---
    for rec in tokens.values():
        row = [
            rec['mint'],
            f"{rec['spent_sol']} SOL", f"{rec['earned_sol']} SOL",
            f"{rec['delta_sol']} SOL", f"{rec['delta_pct']}%",
            rec['buys'], rec['sells'], rec['last_trade'],
            rec['entry_mcap'], rec['exit_mcap'], None, None,
            rec['entry_mcap'], rec['exit_mcap'], rec['current_mcap'],
            f"https://solscan.io/token/{rec['mint']}",
            f"https://dexscreener.com/solana/token/{rec['mint']}",
            f"https://photon.tools/token/{rec['mint']}"
        ]
        ws.append(row)
        # Окраска Delta Sol и Delta %
        r = ws.max_row
        sol_cell = ws.cell(r, 4)
        pct_cell = ws.cell(r, 5)
        if rec['delta_sol'] > 0:
            color = 'C6EFCE'
        else:
            color = 'FFC7CE'
        sol_cell.fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        pct_cell.fill = PatternFill(start_color=color, end_color=color, fill_type="solid")

    # Сохраняем
    fname = f"report_{summary['wallet']}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(fname)
    return fname

# === Обработчики бота ===
@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "Привет! Пришли Solana-адрес кошелька для отчёта.")

@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    bot.reply_to(message, "Собираю данные, это займет чуть-чуть...")
    try:
        tokens, summary = analyze_wallet(wallet)
        if not tokens:
            return bot.send_message(message.chat.id, "Не найдено транзакций для этого кошелька.")
        path = generate_excel(tokens, summary)
        with open(path, 'rb') as f:
            bot.send_document(message.chat.id, f)
        os.remove(path)
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}")

if __name__ == "__main__":
    bot.remove_webhook()
    threading.Thread(target=bot.polling, daemon=True).start()
    PORT = int(os.getenv('PORT', 10000))
    Handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Запущен HTTP сервер на порту {PORT}")
        httpd.serve_forever()
