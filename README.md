# RoastBot — дерзкий Telegram‑бот с Groq LLM

Деплой без денег: **Render Free Web Service** + пинг каждые 5 минут.

## Содержимое

| Файл            | Назначение                        |
|-----------------|-----------------------------------|
| `bot.py`        | Главный скрипт (бот + aiohttp)    |
| `requirements.txt` | Python‑зависимости            |
| `render.yaml`   | Черёж сервиса для Render          |

## Шаги развёртывания

1. **Клонируйте репозиторий**
   ```bash
   git clone <repo-url-or-local-path>
   cd roastbot
   ```

2. **Настройте PERSONAS**
   Откройте `bot.py`, замените `111111111` и `222222222` на реальные `user.id`
   каждого участника группы и задайте желаемый стиль общения.

3. **Создайте GitHub‑репозиторий** (если ещё нет) и запушьте код:
   ```bash
   git init
   git add .
   git commit -m "Initial RoastBot"
   git remote add origin https://github.com/<your-login>/roastbot.git
   git push -u origin main
   ```

4. **Настройка Render**
   * Sign in с GitHub → **New → Web Service → Connect repo**.
   * Branch: `main`, Environment: *Python*, Plan: **Free**.
   * Build & start commands подхватятся из `render.yaml`.
   * В секции **Environment Variables** добавьте:
     - `TG_TOKEN` — токен от @BotFather  
     - `GROQ_API_KEY` — ваш токен Groq  
     - *(опц.)* `GROQ_MODEL` — например `mixtral-8x7b`
   * Confirm → дождитесь окончания билда и надписи **Live**.

5. **Будильник (UptimeRobot)**
   * Зарегистрируйтесь на <https://uptimerobot.com>.
   * **Add Monitor → HTTP(s)**  
     URL: `https://tg-roastbot.onrender.com/ping`  
     Interval: *5 minutes* — сохранить.

6. **Добавляем бота в группу**
   * Пригласите `@tg_roastbot` в свой чат.
   * В настройках бота → **Group Privacy** выключить (чтобы бот видел все сообщения).

Готово! Бот отвечает мгновенно и не засыпает.

## Локальный запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TG_TOKEN=123456:ABC...
export GROQ_API_KEY=groq-...
python bot.py
```

▸ Откройте другой терминал и отправьте `/ping`:
```bash
curl http://localhost:10000/ping
```

## Лицензия

MIT — свободно используйте и модернизируйте.
