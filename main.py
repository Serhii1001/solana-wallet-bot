import os
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

# Initialize bot and clear any existing webhook and pending updates
bot = telebot.TeleBot(TELEGRAM_TOKEN)
bot.remove_webhook()
# On some versions, drop pending updates explicitly:
bot.delete_webhook(drop_pending_updates=True)

# Helpers

def safe_request(url, timeout=10, retries=2):
    for _ in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException:
            continue
    return {}


def format_date(ts_str):
    dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
    return dt.strftime('%d.%m.%Y')


def format_period(sec_delta):
    secs = int(sec_delta)
    mins, sec = divmod(secs, 60)
    hrs, min_ = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {min_}m"
    if mins:
        return f"{mins} min"
    return f"{sec}s"


def get_transactions(wallet, limit=500):
    txs = []
    before = None
    while len(txs) < limit:
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
        if before:
            url += f"&before={before}"
        batch = safe_request(url)
        if not isinstance(batch, list) or not batch:
            break
        txs.extend(batch)
        before = batch[-1].get('signature')
        if len(batch) < 100:
            break
    return txs


def get_balance(wallet):
    data = safe_request(HELIUS_BALANCE_ENDPOINT.format(wallet=wallet) + f"?api-key={HELIUS_API_KEY}")
    lamports = data.get('nativeBalance', 0) if isinstance(data, dict) else 0
    return round(lamports / 1e9, 4)


def get_mcap(mint):
    data = safe_request(DEXSCREENER_BASE + mint)
    return data.get('stats', {}).get('marketCap', 0)


def analyze_wallet(wallet):
    txs = get_transactions(wallet)
    tokens = {}

    for tx in txs:
        ts = tx.get('timestamp')
        ts_fmt = datetime.utcfromtimestamp(ts/1000 if ts > 1e12 else ts)
        native = tx.get('nativeTransfers', [])
        spent = sum(n['amount']/1e9 for n in native if n.get('fromUserAccount') == wallet)
        earned = sum(n['amount']/1e9 for n in native if n.get('toUserAccount') == wallet)
        fee = tx.get('fee', 0)/1e9

        for tr in tx.get('tokenTransfers', []):
            mint = tr['mint']
            direction = 'buy' if tr.get('toUserAccount') == wallet else 'sell' if tr.get('fromUserAccount') == wallet else None
            if not direction:
                continue
            rec = tokens.setdefault(mint, {
                'mint': mint, 'spent': 0.0, 'earned': 0.0, 'buys': 0, 'sells': 0,
                'first_ts': None, 'last_ts': None, 'first_price': 0.0, 'last_price': 0.0, 'fee': 0.0
            })
            amount = tr.get('tokenAmount', 0)/10**tr.get('decimals', 0)
            if direction == 'buy':
                rec['buys'] += 1
                rec['spent'] += spent
                rec['fee'] += fee
                if not rec['first_ts']:
                    rec['first_ts'] = ts_fmt
                    rec['first_price'] = round(spent/amount, 6) if amount else 0.0
            else:
                rec['sells'] += 1
                rec['earned'] += earned
                rec['fee'] += fee
                rec['last_ts'] = ts_fmt
                rec['last_price'] = round(earned/amount, 6) if amount else 0.0

    # Summary
    total_spent = total_earned = 0.0
    wins = losses = 0
    pcts = []
    for rec in tokens.values():
        s, e = rec['spent'], rec['earned']
        delta = round(e - s, 4)
        pct = round(delta/s*100, 2) if s else 0.0
        rec.update({'delta': delta, 'pct': pct, 'last_trade': rec['last_ts'] or rec['first_ts']})
        rec['mcap_in'] = get_mcap(rec['mint'])
        rec['mcap_out'] = get_mcap(rec['mint'])
        total_spent += s
        total_earned += e
        if delta > 0:
            wins += 1
            pcts.append(pct)
        elif delta < 0:
            losses += 1

    winrate = round(wins/(wins+losses)*100, 2) if wins+losses else 0.0
    avgwin = round(sum(pcts)/len(pcts), 2) if pcts else 0.0
    pnl = round(total_earned - total_spent, 4)
    balance = get_balance(wallet)
    balchg = round((balance - (balance-pnl))/(balance-pnl)*100, 2) if balance else 0.0

    summary = {
        'wallet': wallet,
        'winrate': f"{winrate}%",
        'pnl': f"{pnl} SOL",
        'avgwin': f"{avgwin}%",
        'pnlloss': f"{abs(min(0, pnl))} SOL",
        'balchg': f"{balchg}%",
        'period': '30 days',
        'solprice': f"{SOL_PRICE} $",
        'balance': f"{balance} SOL"
    }
    return tokens, summary


def generate_excel(tokens, summary):
    wb = Workbook()
    ws = wb.active
    # Metadata
    meta_h = ['Wallet', '', '', '', 'WinRate', 'PnL R', '', 'Avg Win %', 'PnL Loss', 'Balance change', '', 'TimePeriod', 'SOL Price Now', 'Balance']
    meta_v = [summary['wallet'], '', '', '', summary['winrate'], summary['pnl'], '', summary['avgwin'], summary['pnlloss'], summary['balchg'], '', summary['period'], summary['solprice'], summary['balance']]
    ws.append(meta_h)
    ws.append(meta_v)
    # Classification rows
    ws.append(['']*len(meta_h))
    ws.append(['Tokens entry MCAP:'])
    ws.append(['<5k', '5k-30k', '30k-100k', '100k-300k', '300k+'])
    ws.append(['']*len(meta_h))
    ws.append(['']*len(meta_h))
    # Table header
    hdr = ['Token', 'Spent SOL', 'Earned SOL', 'Delta Sol', 'Delta %', 'Buys', 'Sells', 'Last trade', 'Income', 'Outcome', 'Fee', 'Period', 'First buy Mcap', 'Last tx Mcap', 'Current Mcap', 'Contract', 'Dexscreener', 'Photon']
    ws.append(hdr)
    for col in range(1, len(hdr)+1):
        cell = ws.cell(row=ws.max_row, column=col)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    # Data rows
    for rec in tokens.values():
        last = rec['last_trade']
        date = last.strftime('%d.%m.%Y') if isinstance(last, datetime) else '-'
        if rec['first_ts'] and rec['last_ts']:
            period = format_period((rec['last_ts'] - rec['first_ts']).total_seconds())
        else:
            period = '-'
        row = [
            rec['mint'], f"{round(rec['spent'],2)} SOL", f"{round(rec['earned'],2)} SOL", f"{rec['delta']} SOL", f"{rec['pct']}%",
            rec['buys'], rec['sells'], date, rec['mcap_in'], rec['mcap_out'], round(rec['fee'],5), period,
            '', '', '', f"https://solscan.io/token/{rec['mint']}", f"https://dexscreener.com/solana/token/{rec['mint']}", f"https://photon.tools/token/{rec['mint']}"
        ]
        ws.append(row)
        color = 'C6EFCE' if rec['delta'] > 0 else 'FFC7CE'
        for col in (4, 5):
            ws.cell(row=ws.max_row, column=col).fill = PatternFill(start_color=color, end_color=color, fill_type="solid")

    fname = f"report_{summary['wallet']}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(fname)
    return fname

# Bot handlers
@bot.message_handler(commands=['start'])
def welcome(m):
    bot.reply_to(m, "Привет! Пришли Solana-адрес.")

@bot.message_handler(func=lambda m: True)
def handle(m):
    wallet = m.text.strip()
    bot.reply_to(m, "Формирую...")
    tokens, summary = analyze_wallet(wallet)
    if not tokens:
        return bot.send_message(m.chat.id, "Не найдено транзакций.")
    path = generate_excel(tokens, summary)
    with open(path, 'rb') as f:
        bot.send_document(m.chat.id, f)
    os.remove(path)

# Run bot with skipping pending updates to avoid getUpdates conflict
# Run bot clearing any pending updates to avoid conflicts
if __name__ == "__main__":
    # Fully reset webhook and pending updates
    bot.remove_webhook()
    bot.delete_webhook(drop_pending_updates=True)
    # Start polling, dropping any pending updates
    bot.infinity_polling(drop_pending_updates=True)
