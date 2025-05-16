def analyze_wallet(wallet):
    # Fetch transactions and balances
    tx_url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"
    txs = safe_request(tx_url) or []
    bal = safe_request(f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}")
    balance = bal.get('nativeBalance', 0) / 1e9

    tokens = {}
    # Process each transaction
    for tx in txs:
        ts = datetime.fromtimestamp(tx.get('timestamp', 0))
        # Determine SOL spent and earned via nativeTransfers
        sol_spent = 0.0
        sol_earned = 0.0
        for nt in tx.get('nativeTransfers', []):
            lamports = nt.get('amount', 0)
            amount_sol = lamports / 1e9
            if nt.get('fromUserAccount') == wallet:
                sol_spent += amount_sol
            if nt.get('toUserAccount') == wallet:
                sol_earned += amount_sol
        # Process token transfers per mint
        # Keep track per mint whether this tx had buy/sell for count consistency
        seen = {}
        for tr in tx.get('tokenTransfers', []):
            mint = tr.get('mint')
            amount = float(tr.get('tokenAmount', 0)) / (10 ** tr.get('decimals', 0))
            direction = ('buy' if tr.get('toUserAccount') == wallet else 
                         'sell' if tr.get('fromUserAccount') == wallet else None)
            if not direction:
                continue
            rec = tokens.setdefault(mint, {
                'mint': mint,
                'symbol': get_symbol(mint),
                'spent_sol': 0,
                'earned_sol': 0,
                'delta_sol': 0,
                'delta_pct': 0,
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
            # Count buys/sells once per tx per mint
            if seen.get((mint, direction)) is None:
                if direction == 'buy':
                    rec['buys'] += 1
                    rec['spent_sol'] += sol_spent
                else:
                    rec['sells'] += 1
                    rec['earned_sol'] += sol_earned
                seen[(mint, direction)] = True
            # Accumulate token amounts
            if direction == 'buy':
                rec['in_tokens'] += amount
                if not rec['first_ts']:
                    rec['first_ts'] = ts
                    rec['first_mcap'] = get_historical_mcap(mint, ts)
            else:
                rec['out_tokens'] += amount
                rec['last_ts'] = ts
                rec['last_mcap'] = get_historical_mcap(mint, ts)
            # Always accumulate fee
            rec['fee'] += tx.get('fee', 0) / 1e9

    # Final metrics
    for rec in tokens.values():
        rec['delta_sol'] = rec['earned_sol'] - rec['spent_sol']
        rec['delta_pct'] = (rec['delta_sol'] / rec['spent_sol'] * 100
                            if rec['spent_sol'] else 0)
        rec['period'] = format_duration(rec['first_ts'], rec['last_ts'])
        rec['last_trade'] = rec['last_ts'] or rec['first_ts']
        rec['current_mcap'] = get_current_mcap(rec['mint'])

    summary = {
        'wallet': wallet,
        'balance': balance,
        'pnl': sum(r['delta_sol'] for r in tokens.values()),
        'avg_win_pct': (sum(r['delta_pct'] for r in tokens.values() if r['delta_sol'] > 0) /
                        max(1, sum(1 for r in tokens.values() if r['delta_sol'] > 0))),
        'pnl_loss': sum(r['delta_sol'] for r in tokens.values() if r['delta_sol'] < 0),
        'balance_change': (sum(r['delta_sol'] for r in tokens.values()) /
                           (balance - sum(r['delta_sol'] for r in tokens.values()) ) * 100
                           if balance else 0),
        'winrate': (sum(1 for r in tokens.values() if r['delta_sol'] > 0) /
                    max(1, sum(1 for r in tokens.values() if abs(r['delta_sol']) > 0)) * 100),
        'time_period': '30 days',
        'sol_price': SOL_PRICE
    }
    return tokens, summary


def generate_excel(wallet, tokens, summary):
    filename = f"{wallet}_report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "ArGhost table"

    # Summary header
    headers = [
        'Wallet', 'WinRate', 'PnL R', 'Avg Win %', 'PnL Loss',
        'Balance change', 'TimePeriod', 'SOL Price Now', 'Balance'
    ]
    for col, title in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=title)
    values = [
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
    for col, val in enumerate(values, start=1):
        ws.cell(row=2, column=col, value=val)

    # Entry MCAP ranges
    ws.cell(row=4, column=1, value='Tokens entry MCAP:')
    ranges = ['<5k', '5k-30k', '30k-100k', '100k-300k', '300k+']
    for idx, rng in enumerate(ranges, start=2):
        ws.cell(row=5, column=idx, value=rng)

    # Table headers
    cols = [
        'Token', 'Spent SOL', 'Earned SOL', 'Delta Sol', 'Delta %', 'Buys',
        'Sells', 'Last trade', 'Income', 'Outcome', 'Fee', 'Period',
        'First buy Mcap', 'Last tx Mcap', 'Current Mcap', 'Contract',
        'Dexscreener', 'Photon'
    ]
    header_row = 8
    for col, title in enumerate(cols, start=1):
        ws.cell(row=header_row, column=col, value=title)

    # Fill table
    row = 9
    for rec in tokens.values():
        ws.cell(row=row, column=1, value=rec['symbol'])
        ws.cell(row=row, column=2, value=f"{rec['spent_sol']:.2f} SOL")
        ws.cell(row=row, column=3, value=f"{rec['earned_sol']:.2f} SOL")
        ws.cell(row=row, column=4, value=f"{rec['delta_sol']:.2f}")
        ws.cell(row=row, column=5, value=f"{rec['delta_pct']:.2f}%")
        ws.cell(row=row, column=6, value=rec['buys'])
        ws.cell(row=row, column=7, value=rec['sells'])
        if rec['last_trade']:
            ws.cell(row=row, column=8, value=rec['last_trade'].strftime('%d.%m.%Y'))
        ws.cell(row=row, column=9, value=rec['in_tokens'])
        ws.cell(row=row, column=10, value=rec['out_tokens'])
        ws.cell(row=row, column=11, value=f"{rec['fee']:.2f}")
        ws.cell(row=row, column=12, value=rec['period'])
        ws.cell(row=row, column=13, value=rec['first_mcap'])
        ws.cell(row=row, column=14, value=rec['last_mcap'])
        ws.cell(row=row, column=15, value=rec['current_mcap'])
        ws.cell(row=row, column=16, value=rec['mint'])
        cell_dex = ws.cell(row=row, column=17)
        cell_dex.value = 'View trades'
        cell_dex.hyperlink = f"https://dexscreener.com/solana/{rec['mint']}?maker={wallet}"
        cell_photo = ws.cell(row=row, column=18)
        cell_photo.value = 'View trades'
        cell_photo.hyperlink = f"https://photon-sol.tinyastro.io/en/lp/{rec['mint']}"
        row += 1

    wb.save(filename)
    return filename


@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "Привет! Отправь Solana-адрес.")


@bot.message_handler(func=lambda m: True)
def handle_wallet(message):
    wallet = message.text.strip()
    bot.reply_to(message, "Обрабатываю...")
    tokens, summary = analyze_wallet(wallet)
    fname = generate_excel(wallet, tokens, summary)
    with open(fname, "rb") as f:
        bot.send_document(message.chat.id, f)


if __name__ == "__main__":
    # Start bot polling in a separate thread
    polling_thread = threading.Thread(target=bot.infinity_polling, daemon=True)
    polling_thread.start()
    # Run Flask app to bind to required port
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
