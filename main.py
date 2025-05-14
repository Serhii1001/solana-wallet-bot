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
HELIUS_MINT_ENDPOINT = "https://api.helius.xyz/v0/mints/{}"

# Initialize bot and clear webhook
bot = telebot.TeleBot(TELEGRAM_TOKEN)
bot.remove_webhook()
try:
    bot.delete_webhook(drop_pending_updates=True)
except TypeError:
    bot.delete_webhook()

# === Helpers ===

def safe_request(url, timeout=10, retries=2):
    for _ in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException:
            pass
    return {}


def get_symbol(mint):
    data = safe_request(HELIUS_MINT_ENDPOINT.format(mint) + f"?api-key={HELIUS_API_KEY}")
    return data.get('symbol') or mint[:6]


def get_historical_mcap(mint, ts_dt):
    """
    Получение исторической MCAP токена из Dexscreener chart API близко к заданному времени.
    Используем интервал 1 час для более точного соответствия.
    """
    url = f"{DEXSCREENER_BASE}{mint}/chart?interval=1h"
    data = safe_request(url)
    points = data.get('chart', [])
    if not points:
        return 0
    target = int(ts_dt.timestamp() * 1000)
    best = min(points, key=lambda p: abs(p.get('timestamp', 0) - target))
    return best.get('marketCap', 0)


def get_current_mcap(mint):
