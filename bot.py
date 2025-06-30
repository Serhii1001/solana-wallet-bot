#!/usr/bin/env python3
import os, logging, random, requests, asyncio
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

# ───────────  ЛОГИ  ───────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ───────────  СОЗДАЁМ application  ───────────
application = (
    ApplicationBuilder()
    .token(os.environ["TG_TOKEN"])     # токен задаётся в переменных окружения
    .concurrent_updates(True)         # обрабатывать апдейты параллельно
    .build()
)

# ───────────  PERSONAS  ───────────
PERSONAS = {
    5839634721: {                       # id друга
        "names": ["Дрюля", "Дрюлькин", "Пидрюлькин"],
        "style": "колкости + мат обязмтелен, писать ему сасай, подкалывать насчёт его тупого юмора"
    },
    769361377: {
        "names": ["Шеф", "Босс", "Золотой"],
        "style": "общатся уважительно"
    },
    622827013: {                       # id друга
        "names": ["Дятько", "Трахачь", "Трахуля", "Дидук", "Трахиладзе", "Трахмастер"],
        "style": "колкости + мат, добавить какие то гейские шутки про него"
    },
    111111112: {
        "names": ["Бодя", "Пёс", "Индюк", "Черномырдин", "Негрюля", "Чернозем", "Гуталин", "Годя"],
        "style": "колкости + мат,  добавить какие то гейские шутки про него, максимально жестко оскорблять, придумывать пошлые гейские истории про него"
    },
    617500468: {
        "names": ["Равлик", "Павлентий", "Павидло", "Бомж бородатый"],
        "style": "колкости + мат, шутки про сахарный диабет и тому подобные, про бороду как у бомжа"
    }
}
alias_idx = defaultdict(int)
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"

# ───────────  ОБРАБОТКА ТЕКСТА  ───────────
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (msg := update.message) or not msg.text:
        return

    user_id, text = msg.from_user.id, msg.text
    logger.info("📥 %s: %s", user_id, text)

    persona = PERSONAS.get(
        user_id, {"names": ["Гость"], "style": "общайся нейтрально"}
    )
    alias = persona["names"][alias_idx[user_id] % len(persona["names"])]
    alias_idx[user_id] += 1

    system_prompt = (
        "Всегда отвечай только на русском языке. Не используй английский или другие языки. Говори коротно, не задавай много вопросов. Можешшь просто иногда отвечать одним словом в рифму - например где? В пизде. "
        f"Ты жестокий гопник, отвечаешь резко, матом, с агрессией и пренебрежением. "
        f"Обращайся к собеседнику «{alias}». "
        f"Скажи грубо, унизительно, язвительно. Используй блатной жаргон, понты, уличную лексику например чёрт, питушара, псина и тому подобные. "
        f"Ты должен доминировать, высмеивать, провоцировать. "
        f"Но не затрагивай семью, религию. {persona['style']}"
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
        logger.exception("Groq request failed")
        answer = "⚠️ Ошибка LLM, попробуйте ещё раз."

    await msg.reply_text(answer)

# ───────────  /start  ───────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здарова бродяги!")

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

# ───────────  aiohttp: /ping + запуск polling  ───────────
async def ping(_: web.Request) -> web.Response:
    return web.Response(text="pong")

async def on_startup(_: web.Application):
    logger.info("🚀 Запускаю polling внутри aiohttp…")
    await application.initialize()
    await application.start()  # ← обязательно запускаем app
    await application.updater.start_polling()  # ← polling после старта

async def on_cleanup(_: web.Application):
    await application.updater.stop()
    await application.stop()
    await application.shutdown()

def main() -> None:
    app = web.Application()
    app.router.add_get("/ping", ping)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=int(os.getenv("PORT", 10000)))

# ───────────  Точка входа  ───────────
if __name__ == "__main__":
    main()
