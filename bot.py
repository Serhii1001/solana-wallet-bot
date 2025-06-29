#!/usr/bin/env python3
import os, asyncio, logging, random, requests
from collections import defaultdict
from aiohttp import web
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# ─────────── ЛОГИ И СОЗДАНИЕ ПРИЛОЖЕНИЯ ───────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

application = (
    ApplicationBuilder()
    .token(os.environ["TG_TOKEN"])
    .concurrent_updates(True)          # параллельная обработка
    .build()
)

# ─────────── ЛИЧНЫЕ ДАННЫЕ / ХАРАКТЕРЫ ───────────
PERSONAS = {
    5839634721: {
        "names": ["Дрюля", "Дрюлькин", "Пидрюлькин"],
        "style": "колкости + мат, но без оскорблений семьи, писать ему сасай, подкалывать насчёт его тупого юмора",
    },
    769361377: {
        "names": ["Сэр", "Шеф", "Босс", "Золотой"],
        "style": "немного уважения, язвить можно",
    },
    622827013: {
        "names": ["Дятько", "Трахачь", "Трахуля", "Дидук", "Трахиладзе", "Трахмастер"],
        "style": "колкости + мат, но без оскорблений семьи, добавить какие-то гейские шутки про него",
    },
    111111112: {
        "names": ["Бодя"],
        "style": "колкости + мат, но без оскорблений семьи, добавить какие-то гейские шутки про него, максимально жестко оскорблять",
    },
    617500468: {
        "names": ["Равлик", "Павлентий", "Павидло", "Бомж бородатый"],
        "style": "колкости + мат, но без оскорблений семьи, шутки про диабет, бороду как у бомжа",
    },
}

alias_idx = defaultdict(int)           # round-robin счётчики
GROQ_URL  = "https://api.groq.com/openai/v1/chat/completions"

# ─────────── ОБРАБОТКА ЛЮБОГО ТЕКСТА ───────────
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        logging.warning("⚠️ Non-text message ignored")
        return

    user_id = update.effective_user.id
    text    = update.message.text
    logging.info(f"📥 {user_id}: {text}")

    # Сообщаем ID, если пользователь новый
    if user_id not in PERSONAS:
        await update.message.reply_text(f"🪪 Новый пользователь: {user_id}")

    persona = PERSONAS.get(user_id, {"names": ["Гость"], "style": "общайся нейтрально"})
    alias   = persona["names"][alias_idx[user_id] % len(persona["names"])]
    alias_idx[user_id] += 1

    system_prompt = (
        "Ты дерзкий, агрессивный Telegram-бот. Мат допустим, но без хейта к защищённым группам. "
        f"Обращайся к собеседнику словом «{alias}». " + persona["style"]
    )

    payload = {
        "model": os.getenv("GROQ_MODEL", "mixtral-8x7b"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ],
    }

    try:
        r = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"]
    except Exception:
        logging.exception("Groq request failed")
        answer = "⚠️ Ошибка LLM, попробуйте ещё раз."

    await update.message.reply_text(answer)

# ─────────── /start ───────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот активен. Готов к работе!")

# регистрируем хэндлеры один раз
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

# ─────────── СТАРТ / ОСТАНОВКА ───────────
async def start_bot(_: web.Application) -> None:
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        logging.error("❌ WEBHOOK_URL не задан!")
        return
    await application.bot.set_webhook(f"{webhook_url}/webhook")
    logging.info(f"✅ Webhook установлен: {webhook_url}/webhook")
    await application.initialize()
    await application.start()

async def ping(_: web.Request) -> web.Response:
    return web.Response(text="pong")

async def cleanup(_: web.Application) -> None:
    await application.stop()
    await application.shutdown()

def main() -> None:
    aio = web.Application()
    aio.router.add_get("/ping", ping)
    aio.router.add_post("/webhook", application.webhook_handler())
    aio.on_startup.append(start_bot)
    aio.on_cleanup.append(cleanup)
    port = int(os.getenv("PORT", 10000))
    web.run_app(aio, port=port)

# ─────────── Точка входа ───────────
if __name__ == "__main__":
    logging.info("🚀 Запуск через aiohttp на Render…")
    main()
