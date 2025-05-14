import os
import requests
import telebot
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

# ---------------- Configuration ----------------
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
SOL_PRICE_ENV = os.getenv("SOL_PRICE") or "0"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Ensure Helius API key is configured
if not HELIUS_API_KEY:
    raise RuntimeError("HELIUS_API_KEY env var not set. Please configure your Helius API key.")

# ---------------- Helper Functions ----------------
def safe_request(url, params=None, retries=3):
    for _ in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            continue
    return {}


def fetch_all_txs(wallet, limit=500, period_cutoff_ts=None):
    """
    Fetch all transactions for a wallet with pagination,
    stopping when transactions older than cutoff timestamp are reached.
    """
    url_base = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
    all_txs = []
    cursor = None
    while True:
        params = {"api-key": HELIUS_API_KEY, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = safe_request(url_base, params)
        txs = resp.get("transactions", resp)
        if not txs:
            break
        for tx in txs:
            ts = tx.get("timestamp", 0)
            if period_cutoff_ts and ts < period_cutoff_ts:
                return all_txs
            all_txs.append(tx)
        cursor = resp.get("nextCursor")
        if not cursor:
            break
    return all_txs


def get_sol_price():
    try:
        price = float(SOL_PRICE_ENV)
        if price > 0:
            return price
    except ValueError:
        pass
    data = safe_request(COINGECKO_PRICE_URL, {"ids": "solana", "vs_currencies": "usd"})
    return data.get("solana", {}).get("usd", 0)


def get_symbol(mint):
    url = f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}"
    data = safe_request(url)
    return data.get("symbol", mint[:6])


def get_historical_mcap(mint, timestamp_ms):
    url = f"{DEXSCREENER_BASE}{mint}/chart?interval=1h"
    data = safe_request(url)
    points = data.get("chart", [])
    if not points:
        return None
    best = min(points, key=lambda p: abs(p.get('timestamp', 0) - timestamp_ms))
    return best.get('marketCap')


def get_current_mcap(mint):
    data = safe_request(DEXSCREENER_BASE + mint)
    return data.get('stats', {}).get('marketCap')


def format_sol(val):
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f} SOL"


def format_pct(val):
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f}%"


def format_duration(start, end):
    if not start or not end:
        return "-"
    delta = end - start
    seconds = int(delta.total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days} days"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"

# ---------------- Analysis ----------------
def analyze_wallet(wallet, period_days=30):
    cutoff = datetime.utcnow().timestamp() - period_days * 86400
    txs = fetch_all_txs(wallet, limit=500, period_cutoff_ts=cutoff)

    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9

    records = {}
    for tx in txs:
        ts = tx.get('timestamp', 0)
        dt = datetime.fromtimestamp(ts)
        fee = tx.get('fee', 0) / 1e9
        native = tx.get('nativeTransfers', [])
        spent_native = sum(n.get('amount', 0) / 1e9 for n in native if n.get('fromUserAccount') == wallet)
        earned_native = sum(n.get('amount', 0) / 1e9 for n in native if n.get('toUserAccount') == wallet)

        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            if tr.get('toUserAccount') == wallet:
                direction = 'buy'
            elif tr.get('fromUserAccount') == wallet:
                direction = 'sell'
            else:
                continue

            rec = records.setdefault(mint, {
                'mint': mint, 'symbol': get_symbol(mint),
                'spent': 0.0, 'earned': 0.0, 'fee': 0.0,
                'buys': 0, 'sells': 0, 'income': 0.0, 'outcome': 0.0,
                'first_ts': None, 'last_ts': None,
                'first_mcap': None, 'last_mcap': None, 'current_mcap': None
            })
            rec['fee'] += fee
            if direction == 'buy':
                rec['buys'] += 1
                rec['income'] += amount
                rec['spent'] += spent_native
                if not rec['first_ts']:
                    rec['first_ts'] = dt
                    rec['first_mcap'] = get_historical_mcap(mint, ts*1000)
            else:
                rec['sells'] += 1
                rec['outcome'] += amount
                rec['earned'] += earned_native
                rec['last_ts'] = dt
                rec['last_mcap'] = get_historical_mcap(mint, ts*1000)

    # finalize metrics
    for rec in records.values():
        rec['delta'] = rec['earned'] - rec['spent']
        rec['delta_pct'] = (rec['delta'] / rec['spent'] * 100) if rec['spent'] else 0
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    total_spent = sum(r['spent'] for r in records.values())
    total_earned = sum(r['earned'] for r in records.values())
    pnl = total_earned - total_spent
    wins = [r for r in records.values() if r['delta'] > 0]
    losses = [r for r in records.values() if r['delta'] < 0]
    winrate = len(wins) / max(len(records), 1) * 100
    avg_win = sum(r['delta_pct'] for r in wins) / max(len(records), 1)
    pnl_loss = sum(r['delta'] for r in losses)
    balance_change = (pnl / total_spent * 100) if total_spent else 0

    summary = {
        'wallet': wallet,
        'balance': balance,
        'time_period': f"{period_days} days",
        'sol_price': get_sol_price(),
        'pnl': pnl,
        'winrate': winrate,
        'avg_win_pct': avg_win,
        'pnl_loss': pnl_loss,
        'balance_change': balance_change
    }
    return records, summary

# ---------------- Excel Generation ----------------
def generate_excel(wallet, records, summary):
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"
    bold = Font(bold=True)
    center = Alignment(horizontal='center', vertical='center')

    # Header
    ws.merge_cells('A1:D1'); ws['A1'] = 'Wallet'; ws['A1'].font = bold
    ws.merge_cells('A2:D2'); ws['A2'] = summary['wallet']
    ws.merge_cells('E1:F1'); ws['E1'] = 'TimePeriod'; ws['E1'].font = bold
    ws.merge_cells('E2:F2'); ws['E2'] = summary['time_period']
    ws.merge_cells('G1:H1'); ws['G1'] = 'SOL Price Now'; ws['G1'].font = bold
    ws.merge_cells('G2:H2'); ws['G2'] = f"{summary['sol_price']} $"
    ws.merge_cells('I1:J1'); ws['I1'] = 'Balance'; ws['I1'].font = bold
    ws.merge_cells('I2:J2'); ws['I2'] = format_sol(summary['balance'])

    # Metrics
    metrics = [
        ('WinRate', format_pct(summary['winrate'])),
        ('PnL R', format_sol(summary['pnl'])),
        ('PnL Loss', format_sol(summary['pnl_loss'])),
        ('Avg Win %', format_pct(summary['avg_win_pct'])),
        ('Balance change', format_pct(summary['balance_change']))
    ]
    col = 11
    for name, value in metrics:
        ws.cell(row=1, column=col, value=name).font = bold
        ws.cell(row=2, column=col, value=value)
        col += 2

    # Empty rows
    ws.append([])
    ws.append([])

    # MCAP headers
    ws.append(['<5k', '5k-30k', '30k-100k', '100k-300k', '300k+'])
    for cell in ws[5][:5]: cell.font = bold
    ws.append([])

    # Table headers
    headers = [
        'Token','Spent SOL','Earned SOL','Delta SOL','Delta %',
        'Buys','Sells','Last trade','Income','Outcome','Fee','Period',
        'First buy Mcap','Last tx Mcap','Current Mcap','Contract','Dexscreener','Photon'
    ]
    ws.append(headers)
    for idx in range(1, len(headers)+1):
        ws.cell(row=8, column=idx).font = bold
        ws.cell(row=8, column=idx).alignment = center

    # Data rows
    sorted_rec = sorted(records.values(), key=lambda r: r['last_trade'] or datetime.min, reverse=True)
    for rec in sorted_rec:
        fields = [
            rec['symbol'], rec['spent'], rec['earned'], rec['delta'], rec['delta_pct'],
            rec['buys'], rec['sells'],
            rec['last_trade'].strftime('%d.%m.%Y') if rec['last_trade'] else '-',
            rec['income'], rec['outcome'], rec['fee'], rec['period'],
            rec['first_mcap'] or 'N/A', rec['last_mcap'] or 'N/A', rec['current_mcap'] or 'N/A',
            rec['mint'],
            f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}",
            f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        ]
        ws.append(fields)

    # Hyperlinks and formatting
    for row in range(9, 9+len(sorted_rec)):
        ws.cell(row=row, column=17).value = 'View trades'
        ws.cell(row=row, column=17).hyperlink = ws.cell(row=row, column=17).value
        ws.cell(row=row, column=18).value = 'View trades'
        ws.cell(row=row, column=18).hyperlink = ws.cell(row=row, column=18).value

    # Auto-fit columns
    for i, col_cells in enumerate(ws.columns, 1):
        length = max(len(str(c.value)) for c in col_cells if c.value)
        ws.column_dimensions[get_column_letter(i)].width = length + 2

    filename = f"{wallet}_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)
    return filename

# ---------------- Bot Handlers ----------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.reply_to(message, 'Привет! Отправь мне Solana-адрес для анализа.')

@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    msg = bot.reply_to(message, 'Обрабатываю ваш запрос...')
    records, summary = analyze_wallet(wallet)
    fname = generate_excel(wallet, records, summary)
    with open(fname, 'rb') as f:
        bot.send_document(message.chat.id, f)
    bot.edit_message_text('Готово! Смотрите отчёт ниже.', chat_id=message.chat.id, message_id=msg.message_id)

if __name__ == '__main__':
    # Ensure no webhook is set to avoid polling conflict
    from telebot import apihelper
    apihelper.delete_webhook(token=TELEGRAM_TOKEN)
    bot.remove_webhook()
    # Start long polling
    bot.infinity_polling()
