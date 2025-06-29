#!/usr/bin/env python3
import os, asyncio, logging, random, requests
from collections import defaultdict
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# ─────────── ЛИЧНЫЕ ДАННЫЕ / ХАРАКТЕРЫ ───────────
PERSONAS = {
    5839634721: {                       # id друга
        "names": ["Дрюля", "Дрюлькин", "Пидрюлькин"],
        "style": "колкости + мат, но без оскорблений семьи, писать ему сасай, подкалывать насчёт его тупого юмора"
    },
    769361377: {
        "names": ["Сэр", "Шеф", "Босс", "Золотой"],
        "style": "немного уважения, язвить можно"
    },
    622827013: {                       # id друга
        "names": ["Дятько", "Трахачь", "Трахуля", "Дидук", "Трахиладзе", "Трахмастер"],
        "style": "колкости + мат, но без оскорблений семьи, добавить какие то гейские шутки про него"
    },
    111111112: {
        "names": ["Бодя"],
        "style": "колкости + мат, но без оскорблений семьи, добавить какие то гейские шутки про него, максимально жестко оскорблять"
    },
    617500468: {
        "names": ["Равлик", "Павлентий", "Павидло", "Бомж бородатый"],
        "style": "колкости + мат, но без оскорблений семьи, шутки про сахарный диабет и тому подобные, про бороду как у бомжа"
    }
}

# round-robin счётчик для имён (можно удалить, если хотите random)
alias_idx = defaultdict(int)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ─────────── ОСНОВНОЙ ХЕНДЛЕР ───────────
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text    = update.effective_message.text or ""

    # 1. Показываем ID, если он ещё не занесён
    if user_id not in PERSONAS:
        info = f"🪪 Новый пользователь: {user_id}"
        await update.message.reply_text(info)
        logging.info(info)
        # дальше ответим нейтрально

    # 2. Берём персоналию (или дефолт)
    persona = PERSONAS.get(
        user_id,
        {"names": ["Гость"], "style": "общайся нейтрально"}
    )
    aliases = persona.get("names") or [persona.get("name")]

    #   — вариант RANDOM —
    # alias = random.choice(aliases)

    #   — вариант ROUND-ROBIN —
    idx    = alias_idx[user_id] % len(aliases)
    alias  = aliases[idx]
    alias_idx[user_id] += 1

    system_prompt = (
        "Ты дерзкий, агрессивный Telegram-бот. "
        "Мат допустим, но без хейта к защищённым группам. "
        f"Обращайся к собеседнику словом «{alias}». "
        + persona["style"]
    )

    payload = {
        "model": os.getenv("GROQ_MODEL", "mixtral-8x7b"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text}
        ]
    }

    try:
        r = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                "Content-Type": "application/json"
            },
            json=payload, timeout=15
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"]
    except Exception:
        logging.exception("Groq request failed")
        answer = "⚠️ Ошибка LLM, попробуйте ещё раз."

    await update.message.reply_text(answer)

# Создаём объект приложения вне функции, чтобы можно было остановить
application = None

async def start_bot(_: web.Application) -> None:
    global application
    application = (ApplicationBuilder()
                   .token(os.environ["TG_TOKEN"])
                   .concurrent_updates(True)
                   .build())

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    await application.initialize()
    await application.start()

async def ping(_: web.Request) -> web.Response:
    return web.Response(text="pong")

async def cleanup(_: web.Application) -> None:
    global application
    if application:
        await application.stop()
        await application.shutdown()

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    aio = web.Application()
    aio.router.add_get("/ping", ping)
    aio.on_startup.append(start_bot)
    aio.on_cleanup.append(cleanup)
    port = int(os.getenv("PORT", 10000))
    web.run_app(aio, port=port)

if __name__ == "__main__":
    main()
