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
    stopping if transactions older than cutoff timestamp.
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
        native_transfers = tx.get('nativeTransfers', [])
        spent_native = sum(n.get('amount', 0) / 1e9 for n in native_transfers if n.get('fromUserAccount') == wallet)
        earned_native = sum(n.get('amount', 0) / 1e9 for n in native_transfers if n.get('toUserAccount') == wallet)

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
                'mint': mint,
                'symbol': get_symbol(mint),
                'spent': 0.0, 'earned': 0.0,
                'fee': 0.0, 'buys': 0, 'sells': 0,
                'income': 0.0, 'outcome': 0.0,
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
                    rec['first_mcap'] = get_historical_mcap(mint, ts * 1000)
            else:
                rec['sells'] += 1
                rec['outome'] += amount
                rec['earned'] += earned_native
                rec['last_ts'] = dt
                rec['last_mcap'] = get_historical_mcap(mint, ts * 1000)

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
