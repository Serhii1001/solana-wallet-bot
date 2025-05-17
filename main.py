import os
import json
import requests
import telebot
from datetime import datetime
from openpyxl import Workbook
from flask import Flask, request

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens/solana/"
SOL_PRICE = os.getenv("SOL_PRICE", "0")

# Initialize Telegram bot and Flask app
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Configure webhook on start (remove any existing)
bot.remove_webhook()
bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# Health check endpoint for Render
@app.route("/", methods=["GET"])
def health_check():
    return "Bot is running", 200

# Telegram webhook route
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = telebot.types.Update.de_json(request.get_data(as_text=True), bot)
    bot.process_new_updates([update])
    return "OK", 200

# Safe HTTP request helper
def safe_request(url, params=None):
    for _ in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return {}

# Symbol lookup
def get_symbol(mint):
    url = f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}"
    return safe_request(url).get("symbol", mint)

# Historical market cap
def get_historical_mcap(mint, ts_dt):
    data = safe_request(f"{DEXSCREENER_BASE}{mint}/chart?interval=1h")
    points = data.get('chart', [])
    if not points:
        return ""
    target = int(ts_dt.timestamp() * 1000)
    best = min(points, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', "")

# Current market cap
def get_current_mcap(mint):
    return safe_request(DEXSCREENER_BASE + mint).get('stats', {}).get('marketCap', "")

# Duration formatter
def format_duration(start, end):
    if not start or not end:
        return "-"
    delta = end - start
    d, rem = divmod(delta.total_seconds(), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d: return f"{int(d)}d {int(h)}h"
    if h: return f"{int(h)}h {int(m)}m"
    if m: return f"{int(m)}m"
    return f"{int(s)}s"

# Analyze wallet trades and PnL
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
        sol_spent = sol_earned = 0
        for nt in tx.get('nativeTransfers', []):
            sol = nt.get('amount', 0) / 1e9
            if nt.get('fromUserAccount') == wallet: sol_spent += sol
            if nt.get('toUserAccount') == wallet: sol_earned += sol
        seen = set()
        for tr in tx.get('tokenTransfers', []):
            mint = tr['mint']; amount = float(tr['tokenAmount'])/(10**tr['decimals'])
            dir = 'buy' if tr.get('toUserAccount')==wallet else 'sell' if tr.get('fromUserAccount')==wallet else None
            if not dir: continue
            rec = tokens.setdefault(mint, {
                'mint': mint, 'symbol': get_symbol(mint), 'spent_sol':0,'earned_sol':0,
                'buys':0,'sells':0,'in_tokens':0,'out_tokens':0,'fee':0,
                'first_ts':None,'last_ts':None,'first_mcap':'','last_mcap':'','current_mcap':''
            })
            key=(mint,dir)
            if key not in seen:
                if dir=='buy': rec['buys']+=1; rec['spent_sol']+=sol_spent
                else: rec['sells']+=1; rec['earned_sol']+=sol_earned
                seen.add(key)
            if dir=='buy':
                rec['in_tokens']+=amount
                if not rec['first_ts']: rec['first_ts']=ts; rec['first_mcap']=get_historical_mcap(mint,ts)
            else:
                rec['out_tokens']+=amount
                rec['last_ts']=ts; rec['last_mcap']=get_historical_mcap(mint,ts)
            rec['fee']+=tx.get('fee',0)/1e9
    for rec in tokens.values():
        rec['delta_sol']=rec['earned_sol']-rec['spent_sol']
        rec['delta_pct']=(rec['delta_sol']/rec['spent_sol']*100 if rec['spent_sol'] else 0)
        rec['period']=format_duration(rec['first_ts'],rec['last_ts'])
        rec['last_trade']=rec['last_ts'] or rec['first_ts']
        rec['current_mcap']=get_current_mcap(rec['mint'])
    summary={'wallet':wallet,'balance':balance,
        'pnl':sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct':sum(r['delta_pct'] for r in tokens.values() if r['delta_sol']>0)/max(1,sum(1 for r in tokens.values() if r['delta_sol']>0)),
        'pnl_loss':sum(r['delta_sol'] for r in tokens.values() if r['delta_sol']<0),
        'balance_change':sum(r['delta_sol'] for r in tokens.values())/((balance-sum(r['delta_sol'] for r in tokens.values())) or 1)*100,
        'winrate':sum(1 for r in tokens.values() if r['delta_sol']>0)/max(1,sum(1 for r in tokens.values() if abs(r['delta_sol'])>0))*100,
        'time_period':'30 days','sol_price':SOL_PRICE}
    return tokens, summary

# Generate Excel report

def generate_excel(wallet,tokens,summary):
    fname=f"{wallet}_report.xlsx"; wb=Workbook(); ws=wb.active; ws.title="ArGhost table"
    hdr=['Wallet','WinRate','PnL R','Avg Win %','PnL Loss','Balance change','TimePeriod','SOL Price Now','Balance']
    for i,t in enumerate(hdr,1): ws.cell(1,i,t)
    vals=[wallet,f"{summary['winrate']:.2f}%",f"{summary['pnl']:.2f} SOL",f"{summary['avg_win_pct']:.2f}%",
          f"{summary['pnl_loss']:.2f} SOL",f"{summary['balance_change']:.2f}%",summary['time_period'],f"{summary['sol_price']} $",f"{summary['balance']:.2f} SOL"]
    for i,v in enumerate(vals,1): ws.cell(2,i,v)
    ws.cell(4,1,'Tokens entry MCAP:'); rngs=['<5k','5k-30k','30k-100k','100k-300k','300k+']
    for i,r in enumerate(rngs,2): ws.cell(5,i,r)
    cols=['Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells','Last trade','Income','Outcome','Fee','Period',
          'First buy Mcap','Last tx Mcap','Current Mcap','Contract','Dexscreener','Photon']
    for i,c in enumerate(cols,1): ws.cell(8,i,c)
    r=9
    for rec in tokens.values():
        ws.cell(r,1,rec['symbol']); ws.cell(r,2,f"{rec['spent_sol']:.2f} SOL"); ws.cell(r,3,f"{rec['earned_sol']:.2f} SOL");
        ws.cell(r,4,f"{rec['delta_sol']:.2f}"); ws.cell(r,5,f"{rec['delta_pct']:.2f}%"); ws.cell(r,6,rec['buys']); ws.cell(r,7,rec['sells']);
        if rec['last_trade']: ws.cell(r,8,rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(r,9,rec['in_tokens']); ws.cell(r,10,rec['out_tokens']); ws.cell(r,11,f"{rec['fee']:.2f}");
        ws.cell(r,12,rec['period']); ws.cell(r,13,rec['first_mcap']); ws.cell(r,14,rec['last_mcap']); ws.cell(r,15,rec['current_mcap']); ws.cell(r,16,rec['mint']);
        d=ws.cell(r,17); d.value='View trades'; d.hyperlink=f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"; p=ws.cell(r,18); p.value='View trades'; p.hyperlink=f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        r+=1
    wb.save(fname); return fname

# Handlers
def welcome(msg): bot.reply_to(msg,"Привет! Отправь Solana-адрес.")
bot.register_message_handler(welcome, commands=['start'])

def handle(msg):
    wallet=msg.text.strip(); bot.reply_to(msg,"Обрабатываю..."); tokens,summary=analyze_wallet(wallet);
    f=generate_excel(wallet,tokens,summary); bot.send_document(msg.chat.id, open(f,'rb'))
bot.register_message_handler(handle, func=lambda m: True)

# Start Flask app
def main(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

if __name__=='__main__': main()
