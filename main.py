DEBUG_CHAT_ID = 1234567890123  # <-- замени на свой Telegram chat_id  # ЗАМЕНИ на свой chat_id

def welcome(m):
    bot.reply_to(m, f"Твой chat_id: {m.chat.id}")

def debug(msg):
    try:
        bot.send_message(DEBUG_CHAT_ID, f"🪵 {msg}")
    except:
        pass

def analyze_wallet(wallet):
    debug(f"Анализируем кошелёк: {wallet}")
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    txs = safe_request(url) or []
    debug(f"Найдено транзакций: {len(txs)}")

    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9
    tokens = {}
    added_tokens = 0

    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        sig = tx.get('signature', 'unknown')
        debug(f"📦 Транзакция {sig} @ {ts}")
        sol_change = sum(n.get('amount', 0) for n in tx.get('nativeTransfers', []) if n.get('fromUserAccount') == wallet) / 1e9

        token_transfers = tx.get('tokenTransfers', [])
        for tr in token_transfers:
            mint = tr.get('mint')
            if not mint:
                continue
            debug(f"🎯 Mint найден: {mint}")

            amt = float(tr.get('tokenAmount', {}).get('uiAmount', 0))
            if amt == 0:
                debug(f"⚠️ Пропущено: amount == 0")
                continue

            direction = None
            if wallet in [tr.get('toUserAccount'), tr.get('userAccount')]:
                direction = 'buy'
            elif wallet in [tr.get('fromUserAccount')]:
                direction = 'sell'

            if direction is None:
                debug(f"⚠️ Пропущено: не определено направление (from={tr.get('fromUserAccount')}, to={tr.get('toUserAccount')})")
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

            if direction == 'buy':
                rec['buys'] += 1
                rec['in_tokens'] += amt
                rec['spent_sol'] += sol_change
                if not rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['sells'] += 1
                rec['out_tokens'] += amt
                rec['earned_sol'] += sol_change
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)

            rec['fee'] += tx.get('fee', 0) / 1e9
            debug(f"✅ {direction}: {amt} токенов, {sol_change:.4f} SOL")
            added_tokens += 1

    for rec in tokens.values():
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol'] / rec['spent_sol'] * 100) if rec['spent_sol'] else 0
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0) / max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0)),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (sum(r['delta_sol'] for r in tokens.values()) / ((balance - sum(r['delta_sol'] for r in tokens.values())) or 1) * 100),
        'winrate': sum(1 for r in tokens.values() if r['delta_sol'] > 0) / max(1, sum(1 for r in tokens.values() if abs(r['delta_sol']) > 0)) * 100,
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }

    debug(f"📈 Всего добавлено токенов в отчёт: {added_tokens}")
    return tokens, summary
