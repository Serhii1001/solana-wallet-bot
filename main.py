import os
import time
import requests
import openpyxl
from openpyxl.styles import Font, Alignment
import telebot
from datetime import datetime, timedelta

# Load environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Your Telegram bot token
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY')  # Your Helius API key

if not BOT_TOKEN or not HELIUS_API_KEY:
    raise ValueError("Please set BOT_TOKEN and HELIUS_API_KEY environment variables.")

bot = telebot.TeleBot(BOT_TOKEN)

# Constants
HELIUS_BASE = "https://api.helius.xyz/v0/enhancedTransactions"
DEFAULT_DAYS = 30


def fetch_transactions(wallet: str, days: int):
    """
    Fetch enhanced transactions for a wallet from Helius API within the last `days` days.
    """
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=days)
    payload = {
        "addresses": [wallet],
        "until": int(end_time.timestamp()),
        "since": int(start_time.timestamp()),
        "includeTransactions": True
    }
    params = {"api-key": HELIUS_API_KEY}
    response = requests.post(HELIUS_BASE, json=payload, params=params)
    response.raise_for_status()
    return response.json()


def analyze_trades(data: dict):
    """
    Analyze trade metrics: PnL, win rate, duration, per token stats.
    """
    trades = []  # list of dicts per completed buy+sell
    token_stats = {}
    balance = 0

    # Simplest logic: track each swap of SPL tokens vs SOL
    for entry in data.get('transactions', []):
        for instr in entry.get('instructions', []):
            if instr.get('program') == 'spl-token':
                # Parse based on amount and mint
                pass
    # For brevity, we assume entries contain 'trade' events:
    # data['trades'] with buy_time, sell_time, token, spent, earned, fee
    for t in data.get('trades', []):
        spent = t['spent']
        earned = t['earned']
        profit = earned - spent
        duration = t['sell_time'] - t['buy_time']
        trades.append({
            'token': t['token'],
            'spent': spent,
            'earned': earned,
            'fee': t.get('fee', 0),
            'profit': profit,
            'duration': duration,
            'delta_percent': (profit / spent * 100) if spent else 0
        })
        balance += profit
        # Aggregate per token
        stats = token_stats.setdefault(t['token'], {
            'spent': 0, 'earned': 0,
            'profit': 0, 'fee': 0,
            'buys': 0, 'sells': 0
        })
        stats['spent'] += spent
        stats['earned'] += earned
        stats['profit'] += profit
        stats['fee'] += t.get('fee', 0)
        stats['buys'] += 1
        stats['sells'] += 1

    win_rate = (sum(1 for t in trades if t['profit'] > 0) / len(trades) * 100) if trades else 0
    pnL_r = balance
    time_period = f"{days} days"

    return {
        'general': {
            'Win Rate (%)': round(win_rate, 2),
            'PnL R': round(pnL_r, 4),
            'TimePeriod': time_period,
            'Balance': round(balance, 4),
        },
        'tokens': token_stats,
        'trades': trades
    }


def generate_excel_report(wallet: str, analysis: dict, period: int):
    """
    Generate an Excel report with openpyxl and return file path.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'General Metrics'

    # Write general metrics
    ws.append(['Wallet', wallet])
    for key, val in analysis['general'].items():
        ws.append([key, val])
    for cell in ws['A'] + ws['B']:
        cell.font = Font(bold=True)
    ws.column_dimensions['A'].width = 20
    ws.column_dimensions['B'].width = 15

    # Token metrics sheet
    ws2 = wb.create_sheet(title='Token Stats')
    headers = ['Token', 'Spent', 'Earned', 'Profit', 'Fee', 'Buys', 'Sells']
    ws2.append(headers)
    for token, stats in analysis['tokens'].items():
        ws2.append([
            token,
            stats['spent'],
            stats['earned'],
            stats['profit'],
            stats['fee'],
            stats['buys'],
            stats['sells'],
        ])
    for col in ws2.columns:
        col[0].font = Font(bold=True)
        ws2.column_dimensions[col[0].column_letter].width = 15

    # Trades sheet
    ws3 = wb.create_sheet(title='Trades')
    headers = ['Token', 'Spent', 'Earned', 'Profit', 'Fee', 'Delta %', 'Duration (s)']
    ws3.append(headers)
    for t in analysis['trades']:
        ws3.append([
            t['token'], t['spent'], t['earned'], t['profit'], t['fee'],
            round(t['delta_percent'], 2), int(t['duration'].total_seconds())
        ])
    for col in ws3.columns:
        col[0].font = Font(bold=True)
        ws3.column_dimensions[col[0].column_letter].width = 15

    # Save file
    filename = f"report_{wallet[:6]}_{period}d_{int(time.time())}.xlsx"
    path = os.path.join('/mnt/data', filename)
    wb.save(path)
    return path


@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, (
        "Привет! Отправь мне адрес Solana-кошелька и период анализа в днях, например:
"
        "`<адрес> 30`",
        parse_mode='Markdown'
    ))


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    parts = message.text.strip().split()
    if len(parts) not in (1, 2):
        return bot.reply_to(message, "Используй формат: `<адрес> [период_в_днях]`.")
    wallet = parts[0]
    try:
        period = int(parts[1]) if len(parts) == 2 else DEFAULT_DAYS
    except ValueError:
        return bot.reply_to(message, "Период должен быть числом в днях.")

    msg = bot.reply_to(message, "Собираю данные... Это может занять минуту.")
    try:
        data = fetch_transactions(wallet, period)
        analysis = analyze_trades(data)
        report_path = generate_excel_report(wallet, analysis, period)
        bot.send_document(message.chat.id, open(report_path, 'rb'), caption="Вот твой отчет 📊")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")
    finally:
        bot.delete_message(msg.chat.id, msg.message_id)

if __name__ == '__main__':
    print("Bot is running...")
    bot.polling(none_stop=True)
