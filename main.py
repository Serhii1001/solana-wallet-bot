import os
import requests
import telebot
from flask import Flask, request
from datetime import datetime
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

# Initialize bot and app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Configure webhook (remove existing, then set new)
bot.remove_webhook()
bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# Health-check endpoint
def health_check():
    return "OK", 200
app.add_url_rule('/', 'health_check', health_check, methods=['GET'])

# Telegram webhook endpoint
def telegram_webhook():
    data = request.get_data(as_text=True)
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return "OK", 200
app.add_url_rule(f'/{TELEGRAM_TOKEN}', 'telegram_webhook', telegram_webhook, methods=['POST'])

# HTTP helper
def safe_request(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"Request error: {e}")
            continue
    return {}

# Helpers
def get_symbol(mint):
    return safe_request(
        f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}"
    ).get('symbol', mint)

def get_historical_mcap(mint, ts):
    chart = safe_request(
        f"{DEXSCREENER_BASE}{mint}/chart?interval=1h"
    ).get('chart', [])
    if not chart:
        return ''
    target = int(ts.timestamp() * 1000)
    best = min(chart, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', '')

def get_current_mcap(mint):
    return safe_request(f"{DEXSCREENER_BASE}{mint}").get('stats', {}).get('marketCap', '')

# Raydium integration
def get_raydium_pool_info(mint):
    data = safe_request("https://api.raydium.io/v2/sdk/liquidity/mainnet.json") or {}
    pools = data.get('official', []) + data.get('unOfficial', [])
    for p in pools:
        if p.get('baseMint') == mint or p.get('quoteMint') == mint:
            return {
                'ray_pool_id':       p.get('lpMint'),
                'ray_base_reserve':  p.get('baseAssetReserve', 0),
                'ray_quote_reserve': p.get('quoteAssetReserve', 0),
                'ray_fee_rate':      p.get('fee', 0),
            }
    return {}

def format_duration(start, end):
    if not start or not end:
        return '-'
    delta = end - start
    days, rem = divmod(delta.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{int(days)}d {int(hours)}h"
    if hours:
        return f"{int(hours)}h {int(minutes)}m"
    if minutes:
        return f"{int(minutes)}m"
    return f"{int(seconds)}s"

# Core logic
def analyze_wallet(wallet):
    txs = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    ) or []
    bal = safe_request(
        f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}"
    )
    balance = bal.get('nativeBalance', 0) / 1e9
    tokens = {}

    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        sol_spent = sol_earned = 0.0
        for nt in tx.get('nativeTransfers', []):
            amount = nt.get('amount', 0) / 1e9
            if nt.get('fromUserAccount') == wallet:
                sol_spent += amount
            if nt.get('toUserAccount') == wallet:
                sol_earned += amount

        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amt = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            direction = (
                'buy'  if tr.get('toUserAccount')   == wallet else
                'sell' if tr.get('fromUserAccount') == wallet else
                None
            )
            if not direction:
                continue

            rec = tokens.setdefault(mint, {
                'mint': mint,
                'symbol': get_symbol(mint),
                'spent_sol': 0,
                'earned_sol': 0,
                'buys': 0,
                'sells': 0,
                'in_tokens': 0,
                'out_tokens': 0,
                'fee': 0,
                'first_ts': None,
                'last_ts': None,
                'first_mcap': '',
                'last_mcap': '',
                'current_mcap': ''
            })

            # Raydium data
            ray = get_raydium_pool_info(mint)
            rec['ray_pool_id']      = ray.get('ray_pool_id', '')
            rec['ray_base_reserve'] = ray.get('ray_base_reserve', 0)
            rec['ray_quote_reserve']= ray.get('ray_quote_reserve', 0)
            rec['ray_fee_rate']     = ray.get('ray_fee_rate', 0)

            # Count every transfer as one trade
            if direction == 'buy':
                rec['buys']      += 1
                rec['in_tokens'] += amt
                rec['spent_sol'] += sol_spent
                if not rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells']      += 1
                rec['out_tokens'] += amt
                rec['earned_sol'] += sol_earned
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)

            rec['fee'] += tx.get('fee', 0) / 1e9

    # Compute period for each token entry
    for rec in tokens.values():
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])

    # Build summary
    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['earned_sol'] - r['spent_sol'] for r in tokens.values()),
        'avg_win_pct': (
            sum((r['earned_sol'] - r['spent_sol']) / r['spent_sol'] * 100
                for r in tokens.values() if r['earned_sol'] > r['spent_sol'])
            / max(1, sum(1 for r in tokens.values() if r['earned_sol'] > r['spent_sol']))
        ),
        'pnl_loss': sum(r['earned_sol'] - r['spent_sol'] for r in tokens.values() if r['earned_sol'] < r['spent_sol']),
        'balance_change': (
            sum(r['earned_sol'] - r['spent_sol'] for r in tokens.values())
            / ((balance - sum(r['earned_sol'] - r['spent_sol'] for r in tokens.values())) or 1) * 100
        ),
        'winrate': (
            sum(1 for r in tokens.values() if (r['earned_sol'] - r['spent_sol']) > 0)
            / max(1, sum(1 for r in tokens.values() if abs(r['earned_sol'] - r['spent_sol']) > 0)) * 100
        ),
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary

# Excel report
def generate_excel(wallet, tokens, summary):
    fn = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"

    # Header rows
    hdr = [
        'Wallet','WinRate','PnL R','Avg Win %','PnL Loss',
        'Balance change','TimePeriod','SOL Price Now','Balance'
    ]
    for i, t in enumerate(hdr, 1): ws.cell(1, i, t)

    vals = [
        wallet,
        f"{summary['winrate']:.2f}%",
        f"{summary['pnl']:.2f} SOL",
        f"{summary['avg_win_pct']:.2f}%",
        f"{summary['pnl_loss']:.2f} SOL",
        f"{summary['balance_change']:.2f}%",
        summary['time_period'],
        f"{summary['sol_price']} $",
        f"{summary['balance']:.2f} SOL"
    ]
    for i, v in enumerate(vals, 1): ws.cell(2, i, v)

    ws.cell(4, 1, 'Tokens entry MCAP:')
    ranges = ['<5k','5k-30k','30k-100k','100k-300k','300k+']
    for i, r in enumerate(ranges, 2): ws.cell(5, i, r)

    # Token table header
    cols = [
        'Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells',
        'Last trade','Income','Outcome','Fee','Period','First buy Mcap','Last tx Mcap',
        'Current Mcap','Contract','Dexscreener','Photon',
        'Ray Pool ID','Base Reserve','Quote Reserve','Fee Rate'
    ]
    for i, c in enumerate(cols, 1): ws.cell(8, i, c)

    # Fill rows
    r = 9
    for rec in tokens.values():
        ws.cell(r, 1, rec['symbol'])
        ws.cell(r, 2, f"{rec['spent_sol']:.2f} SOL")
        ws.cell(r, 3, f"{rec['earned_sol']:.2f} SOL")
        ws.cell(r, 4, f"{(rec['earned_sol']-rec['spent_sol']):.2f}")
        delta_pct = ((rec['earned_sol']-rec['spent_sol']) / rec['spent_sol'] * 100) if rec['spent_sol'] else 0
        ws.cell(r, 5, f"{delta_pct:.2f}%")
        ws.cell(r, 6, rec['buys'])
        ws.cell(r, 7, rec['sells'])
        if rec.get('last_ts'):
            ws.cell(r, 8, rec['last_ts'].strftime('%d.%m.%Y'))
        ws.cell(r, 9, rec['in_tokens'])
        ws.cell(r, 10, rec['out_tokens'])
        ws.cell(r, 11, f"{rec['fee']:.2f}")
        ws.cell(r, 12, rec.get('period', '-'))
        ws.cell(r, 13, rec['first_mcap'])
        ws.cell(r, 14, rec['last_mcap'])
        ws.cell(r, 15, rec['current_mcap'])
        ws.cell(r, 16, rec['mint'])
        link1 = ws.cell(r, 17)
        link1.value = 'View trades'
        link1.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        link2 = ws.cell(r, 18)
        link2.value = 'View trades'
        link2.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"

        # Raydium fields
        ws.cell(r, 19, rec.get('ray_pool_id', ''))
        ws.cell(r, 20, f"{rec.get('ray_base_reserve', 0):.2f}")
        ws.cell(r, 21, f"{rec.get('ray_quote_reserve', 0):.2f}")
        ws.cell(r, 22, f"{rec.get('ray_fee_rate', 0):.2f}%")

        r += 1

    wb.save(fn)
    return fn

# Handlers
def welcome(m):
    bot.reply_to(m, "Привет! Отправь Solana-адрес.")

@bot.message_handler(func=lambda message: True)
def handle(m):
    wallet = m.text.strip()
    bot.reply_to(m, "Обрабатываю...")
    try:
        tokens, summary = analyze_wallet(wallet)
        if not tokens:
            bot.reply_to(m, "Не найдено сделок для этого адреса или произошла ошибка.")
            return
        report_file = generate_excel(wallet, tokens, summary)
        with open(report_file, 'rb') as doc:
            bot.send_document(m.chat.id, doc)
    except Exception as e:
        print(f"Error handling wallet {wallet}: {e}")
        bot.reply_to(m, f"Произошла ошибка при обработке: {e}")

# Run app
def main():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

if __name__ == '__main__':
    main()
