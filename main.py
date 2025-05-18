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
USE_POLLING      = os.getenv("USE_POLLING", "False").lower() in ("true", "1")

# Initialize bot and app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# HTTP helper
def safe_request(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"Request error: {e}")
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

# Format duration utility
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
    ) or {}
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
            rec.update({
                'ray_pool_id':       ray.get('ray_pool_id', ''),
                'ray_base_reserve':  ray.get('ray_base_reserve', 0),
                'ray_quote_reserve': ray.get('ray_quote_reserve', 0),
                'ray_fee_rate':      ray.get('ray_fee_rate', 0)
            })

            # Count trade
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

    # Post-process tokens
    for rec in tokens.values():
        rec['delta_sol']    = rec.get('earned_sol', 0) - rec.get('spent_sol', 0)
        rec['delta_pct']    = (rec['delta_sol'] / rec['spent_sol'] * 100) if rec['spent_sol'] else 0
        rec['period']       = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade']   = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    # Build summary
    pnl_total = sum(r['delta_sol'] for r in tokens.values())
    wins = [r['delta_sol'] for r in tokens.values() if r['delta_sol'] > 0]
    losses = [r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0]
    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': pnl_total,
        'avg_win_pct': (sum(r['delta_pct'] for r in tokens.values() if r['delta_sol']>0)
                        / max(1, len(wins))),
        'pnl_loss': sum(losses),
        'balance_change': (pnl_total / ((balance - pnl_total) or 1) * 100),
        'winrate': (len(wins) / max(1, len(wins) + len(losses)) * 100),
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

    # Header
    headers = ['Wallet','WinRate','PnL R','Avg Win %','PnL Loss',
               'Balance change','TimePeriod','SOL Price Now','Balance']
    for i, h in enumerate(headers, start=1): ws.cell(1, i, h)
    values = [wallet, f"{summary['winrate']:.2f}%", f"{summary['pnl']:.2f} SOL",
              f"{summary['avg_win_pct']:.2f}%", f"{summary['pnl_loss']:.2f} SOL",
              f"{summary['balance_change']:.2f}%", summary['time_period'],
              f"{summary['sol_price']} $", f"{summary['balance']:.2f} SOL"]
    for i, v in enumerate(values, start=1): ws.cell(2, i, v)

    # Token table
    cols = ['Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells',
            'Last trade','Income','Outcome','Fee','Period','First buy Mcap','Last tx Mcap',
            'Current Mcap','Contract','Dexscreener','Photon',
            'Ray Pool ID','Base Reserve','Quote Reserve','Fee Rate']
    for i, c in enumerate(cols, start=1): ws.cell(8, i, c)

    for row_idx, rec in enumerate(tokens.values(), start=9):
        ws.cell(row_idx, 1, rec['symbol'])
        ws.cell(row_idx, 2, f"{rec['spent_sol']:.2f} SOL")
        ws.cell(row_idx, 3, f"{rec['earned_sol']:.2f} SOL")
        ws.cell(row_idx, 4, f"{rec['delta_sol']:.2f}")
        ws.cell(row_idx, 5, f"{rec['delta_pct']:.2f}%")
        ws.cell(row_idx, 6, rec['buys'])
        ws.cell(row_idx, 7, rec['sells'])
        if rec['last_trade']: ws.cell(row_idx, 8, rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(row_idx, 9, rec['in_tokens'])
        ws.cell(row_idx, 10, rec['out_tokens'])
        ws.cell(row_idx, 11, f"{rec['fee']:.2f}")
        ws.cell(row_idx, 12, rec['period'])
        ws.cell(row_idx, 13, rec['first_mcap'])
        ws.cell(row_idx, 14, rec['last_mcap'])
        ws.cell(row_idx, 15, rec['current_mcap'])
        ws.cell(row_idx, 16, rec['mint'])
        # Hyperlinks
        link1 = ws.cell(row_idx, 17)
        link1.value = 'Dexscreener'
        link1.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        link2 = ws.cell(row_idx, 18)
        link2.value = 'Photon'
        link2.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        # Raydium
        ws.cell(row_idx, 19, rec.get('ray_pool_id', ''))
        ws.cell(row_idx, 20, f"{rec.get('ray_base_reserve',0):.2f}")
        ws.cell(row_idx, 21, f"{rec.get('ray_quote_reserve',0):.2f}")
        ws.cell(row_idx, 22, f"{rec.get('ray_fee_rate',0):.2f}%")

    wb.save(fn)
    return fn

# HTTP webhook for Telegram
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.get_data(as_text=True)
    print(f"Webhook received: {data}")
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return 'OK', 200

# Message handlers
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id, "Привет! Отправь Solana-адрес.")

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    wallet = message.text.strip()
    chat_id = message.chat.id
    bot.send_message(chat_id, "Обрабатываю... 🛠️")
    try:
        tokens, summary = analyze_wallet(wallet)
        if not tokens:
            bot.send_message(chat_id, "Не найдено сделок или произошла ошибка.")
            return
        report = generate_excel(wallet, tokens, summary)
        with open(report, 'rb') as f:
            bot.send_document(chat_id, f)
        bot.send_message(chat_id, f"Готово, отчёт отправлен: {report}")
    except Exception as e:
        print(f"Error processing {wallet}: {e}")
        bot.send_message(chat_id, f"Ошибка при обработке: {e}")

# Run server or polling
if __name__ == '__main__':
    if USE_POLLING:
        print("Starting bot with polling mode...")
        bot.remove_webhook()
        bot.polling(non_stop=True)
    else:
        # webhook mode
        print("Starting Flask server for webhook mode...")
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
