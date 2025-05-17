import os
import requests
import telebot
from datetime import datetime
from openpyxl import Workbook

# Configuration
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY")
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/"
SOL_PRICE        = os.getenv("SOL_PRICE", "0")

# Initialize bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)
bot.remove_webhook()  # Ensure no webhook is active, allow polling(TELEGRAM_TOKEN)

# HTTP helper
def safe_request(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            continue
    return {}

# Helpers
def get_symbol(mint):
    return safe_request(f"https://api.helius.xyz/v0/mints/{mint}?api-key={HELIUS_API_KEY}").get('symbol', mint)

def get_historical_mcap(mint, ts):
    chart = safe_request(f"{DEXSCREENER_BASE}tokens/solana/{mint}/chart?interval=1h").get('chart', [])
    if not chart:
        return ''
    target = int(ts.timestamp()*1000)
    best = min(chart, key=lambda p: abs(p.get('timestamp',0)-target))
    return best.get('marketCap','')

def get_current_mcap(mint):
    return safe_request(f"{DEXSCREENER_BASE}tokens/solana/{mint}").get('stats',{}).get('marketCap','')

def format_duration(start,end):
    if not start or not end:
        return '-'
    delta=end-start
    days,hrem=divmod(delta.total_seconds(),86400)
    hrs,mrem=divmod(hrem,3600)
    mins,secs=divmod(mrem,60)
    if days: return f"{int(days)}d {int(hrs)}h"
    if hrs:  return f"{int(hrs)}h {int(mins)}m"
    if mins: return f"{int(mins)}m"
    return f"{int(secs)}s"

# Analyze wallet

def analyze_wallet(wallet):
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance',0)/1e9
    # find all unique mints
    txs = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100") or []
    mints=set()
    for tx in txs:
        for tr in tx.get('tokenTransfers',[]):
            if tr.get('mint') and (tr.get('toUserAccount')==wallet or tr.get('fromUserAccount')==wallet):
                mints.add(tr['mint'])
    tokens={}
    for mint in mints:
        rec={'mint':mint,'symbol':get_symbol(mint),'spent_sol':0,'earned_sol':0,'delta_sol':0,'delta_pct':0,
             'buys':0,'sells':0,'in_tokens':0,'out_tokens':0,'fee':0,
             'first_ts':None,'last_ts':None,'first_mcap':'','last_mcap':'','current_mcap':''}
        data = safe_request(f"{DEXSCREENER_BASE}trades/solana/{mint}?maker={wallet}")
        for t in data.get('trades',[]):
            side=t.get('side'); ts=datetime.fromtimestamp(t.get('timestamp',0)/1000)
            amt_tok=float(t.get('amount',0)); amt_quote=float(t.get('amountQuote',0))/1e9
            if side=='buy':
                rec['buys']+=1; rec['spent_sol']+=amt_quote; rec['in_tokens']+=amt_tok
                if rec['first_ts'] is None or ts<rec['first_ts']:
                    rec['first_ts']=ts; rec['first_mcap']=get_historical_mcap(mint,ts)
            else:
                rec['sells']+=1; rec['earned_sol']+=amt_quote; rec['out_tokens']+=amt_tok
                if rec['last_ts'] is None or ts>rec['last_ts']:
                    rec['last_ts']=ts; rec['last_mcap']=get_historical_mcap(mint,ts)
        rec['fee']=0; rec['current_mcap']=get_current_mcap(mint)
        rec['delta_sol']=rec['earned_sol']-rec['spent_sol']
        rec['delta_pct']=(rec['delta_sol']/rec['spent_sol']*100) if rec['spent_sol'] else 0
        rec['period']=format_duration(rec['first_ts'],rec['last_ts'])
        rec['last_trade']=rec['last_ts'] or rec['first_ts']
        tokens[mint]=rec
    summary={'wallet':wallet,'balance':balance,'pnl':sum(r['delta_sol'] for r in tokens.values()),
             'avg_win_pct':sum(r['delta_pct'] for r in tokens.values() if r['delta_sol']>0)/max(1,sum(1 for r in tokens.values() if r['delta_sol']>0)),
             'pnl_loss':sum(r['delta_sol'] for r in tokens.values() if r['delta_sol']<0),
             'balance_change':sum(r['delta_sol'] for r in tokens.values())/((balance-sum(r['delta_sol'] for r in tokens.values())) or 1)*100,
             'winrate':sum(1 for r in tokens.values() if r['delta_sol']>0)/max(1,sum(1 for r in tokens.values() if abs(r['delta_sol'])>0))*100,
             'time_period':'30 days','sol_price':SOL_PRICE}
    return tokens, summary

# Generate Excel report
def generate_excel(wallet,tokens,summary):
    fn=f"{wallet}_report.xlsx"; wb=Workbook(); ws=wb.active; ws.title="ArGhost table"
    headers=['Wallet','WinRate','PnL R','Avg Win %','PnL Loss','Balance change','TimePeriod','SOL Price Now','Balance']
    for i,h in enumerate(headers,1): ws.cell(row=1,column=i,value=h)
    vals=[wallet,f"{summary['winrate']:.2f}%",f"{summary['pnl']:.2f} SOL",f"{summary['avg_win_pct']:.2f}%",f"{summary['pnl_loss']:.2f} SOL",f"{summary['balance_change']:.2f}%",summary['time_period'],f"{summary['sol_price']} $",f"{summary['balance']:.2f} SOL"]
    for i,v in enumerate(vals,1): ws.cell(row=2,column=i,value=v)
    ws.cell(row=4,column=1,value='Tokens entry MCAP:'); ranges=['<5k','5k-30k','30k-100k','100k-300k','300k+']
    for i,r in enumerate(ranges,2): ws.cell(row=5,column=i,value=r)
    cols=['Token','Spent SOL','Earned SOL','Delta Sol','Delta %','Buys','Sells','Last trade','Income','Outcome','Fee','Period','First buy Mcap','Last tx Mcap','Current Mcap','Contract','Dexscreener','Photon']
    for i,c in enumerate(cols,1): ws.cell(row=8,column=i,value=c)
    row=9
    for rec in tokens.values():
        ws.cell(row,row,rec['symbol']); ws.cell(row,2,f"{rec['spent_sol']:.2f} SOL"); ws.cell(row,3,f"{rec['earned_sol']:.2f} SOL"); ws.cell(row,4,f"{rec['delta_sol']:.2f}"); ws.cell(row,5,f"{rec['delta_pct']:.2f}%"); ws.cell(row,6,rec['buys']); ws.cell(row,7,rec['sells'])
        if rec['last_trade']: ws.cell(row,8,rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(row,9,rec['in_tokens']); ws.cell(row,10,rec['out_tokens']); ws.cell(row,11,f"{rec['fee']:.2f}"); ws.cell(row,12,rec['period']); ws.cell(row,13,rec['first_mcap']); ws.cell(row,14,rec['last_mcap']); ws.cell(row,15,rec['current_mcap']); ws.cell(row,16,rec['mint']); d=ws.cell(row,17); d.value='View trades'; d.hyperlink=f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"; p=ws.cell(row,18); p.value='View trades'; p.hyperlink=f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"; row+=1
    wb.save(fn); return fn

# Telegram handlers
@bot.message_handler(commands=['start'])
def cmd_start(msg): bot.reply_to(msg,'Привет! Отправь Solana-адрес.')

@bot.message_handler(func=lambda m: True)
def handle_wallet(msg):
    wallet=msg.text.strip(); bot.reply_to(msg,'Обрабатываю...')
    tokens,summary=analyze_wallet(wallet)
    fn=generate_excel(wallet,tokens,summary)
    with open(fn,'rb') as f: bot.send_document(msg.chat.id,f)

# Start polling
if __name__=='__main__': bot.infinity_polling()
