import os
import json
import requests
import telebot
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from datetime import datetime

# === Configuration ===
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
SOL_PRICE = float(os.getenv("SOL_PRICE", 0))  # Цена SOL в USD
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
HELIUS_BALANCE_ENDPOINT = "https://api.helius.xyz/v0/addresses/{wallet}/balances"

# === Инициализация бота ===
bot = telebot.TeleBot(TELEGRAM_TOKEN)
# Убедимся, что нет активного webhook (чтобы не было конфликта getUpdates)
bot.delete_webhook()

# === Вспомогательные функции ===

def safe_request(url, timeout=10, retries=3):
    """HTTP-запрос с таймаутами и повторными попытками"""
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except requests.exceptions.RequestException:
            continue
    return None


def format_datetime(ts):
    if ts > 1e12:
        ts = ts / 1000
    return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


def format_duration_seconds(seconds):
    sec = int(seconds)
    minutes, s = divmod(sec, 60)
    hours, m = divmod(minutes, 60)
    days, h = divmod(hours, 24)
    if days:
        return f"{days}d {h}h {m}m"
    if hours:
        return f"{h}h {m}m"
    if minutes:
        return f"{m}m {s}s"
    return f"{s}s"


def get_mcap(mint):
    data = safe_request(DEXSCREENER_BASE + mint)
    return data.get('stats', {}).get('marketCap') if data else None


def get_transactions(wallet, limit=500):
    all_txs = []
    before = None
    while len(all_txs) < limit:
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
        if before:
            url += f"&before={before}"
        batch = safe_request(url)
        if not batch or not isinstance(batch, list):
            break
        all_txs.extend(batch)
        if len(batch) < 100:
            break
        before = batch[-1].get('signature')
    return all_txs


def get_wallet_balance(wallet):
    url = HELIUS_BALANCE_ENDPOINT.format(wallet=wallet) + f"?api-key={HELIUS_API_KEY}"
    data = safe_request(url)
    lamports = data.get('nativeBalance', 0) if isinstance(data, dict) else 0
    return round(lamports / 1e9, 4)


def analyze_wallet(wallet):
    txs = get_transactions(wallet)
    tokens = {}
    total_spent = total_earned = 0.0
    wins = losses = 0
    profit_percents = []

    for tx in txs:
        ts = tx.get('timestamp')
        ts_str = format_datetime(ts)
        native = tx.get('nativeTransfers', [])
        sol_spent = sum(n.get('amount', 0)/1e9 for n in native if n.get('fromUserAccount') == wallet)
        sol_earned = sum(n.get('amount', 0)/1e9 for n in native if n.get('toUserAccount') == wallet)
        fee = tx.get('fee', 0)/1e9 if tx.get('fee') else 0.0

        for tr in tx.get('tokenTransfers', []):
            mint = tr['mint']
            direction = 'buy' if tr.get('toUserAccount') == wallet else 'sell' if tr.get('fromUserAccount') == wallet else None
            if not direction:
                continue
            amount = tr.get('tokenAmount',0)/10**tr.get('decimals',0)
            rec = tokens.setdefault(mint, {
                'mint': mint, 'spent_sol': 0.0, 'earned_sol': 0.0,
                'buys': 0, 'sells': 0,
                'first_buy_ts': None, 'last_sell_ts': None,
                'first_price': None, 'last_price': None,
                'first_fee': 0.0, 'last_fee': 0.0
            })
            if direction == 'buy':
                rec['buys'] += 1
                rec['spent_sol'] += sol_spent
                rec['first_fee'] += fee
                if not rec['first_buy_ts']:
                    rec['first_buy_ts'] = ts_str
                    rec['first_price'] = round(sol_spent/amount, 6) if amount else 0
            else:
                rec['sells'] += 1
                rec['earned_sol'] += sol_earned
                rec['last_fee'] += fee
                rec['last_sell_ts'] = ts_str
                rec['last_price'] = round(sol_earned/amount, 6) if amount else 0

    for rec in tokens.values():
        spent, earned = rec['spent_sol'], rec['earned_sol']
        delta = earned - spent
        pct = round(delta/spent*100,2) if spent else 0
        rec.update({
            'delta_sol': round(delta,4),
            'delta_pct': pct,
            'last_trade': rec['last_sell_ts'] or rec['first_buy_ts']
        })
        rec['entry_mcap'] = get_mcap(rec['mint'])
        rec['exit_mcap'] = rec['entry_mcap']
        rec['current_mcap'] = get_mcap(rec['mint'])
        total_spent += spent
        total_earned += earned
        if delta > 0:
            wins += 1
            profit_percents.append(pct)
        elif delta < 0:
            losses += 1

    winrate = round(100*wins/(wins+losses),2) if wins+losses else 0
    avg_win = round(sum(profit_percents)/len(profit_percents),2) if profit_percents else 0
    pnl = round(total_earned - total_spent,4)
    current_balance = get_wallet_balance(wallet)
    balance_pct = round((current_balance - (current_balance - pnl))/(current_balance - pnl)*100,2) if pnl and current_balance else 0

    summary = {
        'wallet': wallet,
        'winrate': winrate,
        'pnl_r': pnl,
        'avg_win_pct': avg_win,
        'pnl_loss': round(abs(min(0,pnl)),4),
        'balance_change_pct': balance_pct,
        'time_period': '30 days',
        'sol_price': f"{SOL_PRICE} $",
        'balance_sol': f"{current_balance} SOL"
    }
    return tokens, summary


def generate_excel(tokens, summary):
    wb = Workbook()
    ws = wb.active
    headers1 = ['Wallet', None, None, None, 'WinRate', 'PnL R', None, 'Avg Win %', 'PnL Loss', 'Balance change', None, 'TimePeriod', 'SOL Price Now', 'Balance']
    vals1 = [
        summary['wallet'], None, None, None,
        f"{summary['winrate']}%", f"{summary['pnl_r']} SOL", None,
        f"{summary['avg_win_pct']}%", f"{summary['pnl_loss']} SOL", f"{summary['balance_change_pct']}%",
        None, summary['time_period'], summary['sol_price'], summary['balance_sol']
    ]
    ws.append(headers1)
    ws.append(vals1)
    ws.append([None]*len(headers1))
    ws.append(['Tokens entry MCAP:'])
    ws.append(['<5k','5k-30k','30k-100k','100k-300k','300k+'])
    ws.append([None]*len(headers1))
    ws.append([None]*len(headers1))

    hdrs = ['Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells','Last trade','Income','Outcome','Fee','Period', 'First buy Mcap','Last tx Mcap','Current Mcap','Contract','Dexscreener','Photon']
    ws.append(hdrs)
    for c in range(1, len(hdrs)+1):
        cell = ws.cell(ws.max_row, c)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    for rec in tokens.values():
        if rec['first_buy_ts'] and rec['last_sell_ts']:
            dt1 = datetime.strptime(rec['first_buy_ts'], '%Y-%m-%d %H:%M:%S')
            dt2 = datetime.strptime(rec['last_sell_ts'], '%Y-%m-%d %H:%M:%S')
            period = format_duration_seconds((dt2 - dt1).total_seconds())
        else:
            period = '-'
        row = [
            rec['mint'], f"{rec['spent_sol']} SOL", f"{rec['earned_sol']} SOL", f"{rec['delta_sol']} SOL", f"{rec['delta_pct']}%",
            rec['buys'], rec['sells'], rec['last_trade'], rec['first_price'], rec['last_price'],
            f"{rec['first_fee']} SOL/{rec['last_fee']} SOL", period,
            rec['entry_mcap'], rec['exit_mcap'], rec['current_mcap'],
            f"https://solscan.io/token/{rec['mint']}", f"https://dexscreener.com/solana/token/{rec['mint']}", f"https://photon.tools/token/{rec['mint']}"
        ]
        ws.append(row)
        color = 'C6EFCE' if rec['delta_sol'] > 0 else 'FFC7CE'
        for col in (4, 5):
            ws.cell(ws.max_row, col).fill = PatternFill(start_color=color, end_color=color, fill_type="solid")

    fname = f"report_{summary['wallet']}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(fname)
    return fname

# === Обработчики бота ===
@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "Привет! Пришли Solana-кошелек для отчёта.")

@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    bot.reply_to(message, "Формирую отчёт, подожди...")
    try:
        tokens, summary = analyze_wallet(wallet)
        if not tokens:
            return bot.send_message(message.chat.id, "Не найдено транзакций.")
        path = generate_excel(tokens, summary)
        with open(path, 'rb') as f:
            bot.send_document(message.chat.id, f)
        os.remove(path)
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}")

# === Запуск бота ===
if __name__ == "__main__":
    # Запуск polling (блокирующий вызов), конфликт getUpdates не возникнет
    bot.infinity_polling()
