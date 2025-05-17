import os
import time
import requests
import openpyxl
from openpyxl.styles import Font
import telebot
from datetime import datetime, timedelta

# Environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Telegram Bot Token
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY')  # Helius API Key
if not BOT_TOKEN or not HELIUS_API_KEY:
    raise ValueError("Please set BOT_TOKEN and HELIUS_API_KEY environment variables.")

# Initialize bot and remove any existing webhook to enable polling
bot = telebot.TeleBot(BOT_TOKEN)
bot.delete_webhook()
time.sleep(1)

# Constants
DEFAULT_DAYS = 30
LAMPORTS_PER_SOL = 1e9
HELIUS_BASE = "https://api.helius.xyz/v0/addresses"


def fetch_txns(wallet: str, days: int) -> dict:
    """
    Fetch enhanced transaction history for a wallet from Helius API within the last `days` days.
    """
    url = f"{HELIUS_BASE}/{wallet}/transactions"
    params = {"api-key": HELIUS_API_KEY}
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()  # list of transaction objects

    # Filter by timestamp
    since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    filtered = [tx for tx in data if tx.get('timestamp', 0) >= since_ts]
    return {'transactions': filtered}


def analyze_trades(data: dict, wallet: str):
    """
    Analyze transactions: compute SOL delta per transaction and summary metrics.
    """
    txns = data.get('transactions', [])
    metrics = []
    total_in = 0.0
    total_out = 0.0
    net = 0.0

    for entry in txns:
        timestamp = entry.get('timestamp', 0)
        date = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
        sig = entry.get('signature')
        slot = entry.get('slot')
        fee_sol = entry.get('fee', 0) / LAMPORTS_PER_SOL

        # Determine native SOL change for the wallet
        delta_lamports = 0
        for acct in entry.get('accountData', []):
            if acct.get('account') == wallet:
                delta_lamports = acct.get('nativeBalanceChange', 0)
                break
        delta = delta_lamports / LAMPORTS_PER_SOL

        metrics.append({
            'date': date,
            'signature': sig,
            'slot': slot,
            'delta': delta,
            'fee': fee_sol
        })
        if delta > 0:
            total_in += delta
        else:
            total_out += abs(delta)
        net += delta

    general = {
        'Total Transactions': len(metrics),
        'Total In (SOL)': round(total_in, 6),
        'Total Out (SOL)': round(total_out, 6),
        'Net Change (SOL)': round(net, 6)
    }
    return general, metrics


def generate_excel_report(wallet: str, general: dict, metrics: list, period: int) -> str:
    """
    Generate an Excel report and return filepath.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'General'
    ws.append(['Wallet', wallet])
    for k, v in general.items():
        ws.append([k, v])
    for cell in ws['A'] + ws['B']:
        cell.font = Font(bold=True)
    ws.column_dimensions['A'].width = 25
    ws.column_dimensions['B'].width = 20

    ws2 = wb.create_sheet(title='Transactions')
    ws2.append(['Date', 'Signature', 'Slot', 'Delta (SOL)', 'Fee (SOL)'])
    for m in metrics:
        ws2.append([m['date'], m['signature'], m['slot'], round(m['delta'], 6), round(m['fee'], 6)])
    for col in ws2.columns:
        col[0].font = Font(bold=True)
        ws2.column_dimensions[col[0].column_letter].width = 25

    filename = f"report_{wallet[:6]}_{period}d_{int(time.time())}.xlsx"
    path = os.path.join('/mnt/data', filename)
    wb.save(path)
    return path


@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(
        message,
        "Привет! Отправь мне Solana-адрес и период в днях (по умолчанию 30), например:\n`<wallet> 30`",
        parse_mode='Markdown'
    )


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    parts = message.text.strip().split()
    if not parts:
        return
    wallet = parts[0]
    try:
        period = int(parts[1]) if len(parts) > 1 else DEFAULT_DAYS
    except ValueError:
        return bot.reply_to(message, "Период должен быть числом в днях.")

    info_msg = bot.reply_to(message, "Собираю данные, подожди...")
    try:
        data = fetch_txns(wallet, period)
        general, metrics = analyze_trades(data, wallet)
        report_path = generate_excel_report(wallet, general, metrics, period)
        bot.send_document(message.chat.id, open(report_path, 'rb'), caption="Ваш отчёт готов 📊")
    except Exception as e:
        bot.reply_to(message, f"Ошибка при сборе данных: {e}")
    finally:
        try:
            bot.delete_message(info_msg.chat.id, info_msg.message_id)
        except:
            pass


if __name__ == '__main__':
    print("Bot is running...")
    # Start polling without threading to avoid multiple getUpdates conflicts
    bot.polling(none_stop=True)
