# tg-bot — Личный ИИ-агент в Telegram

## Что это
Telegram-бот на базе Claude API с agent loop и инструментами. Работает 24/7 на Railway.

## Архитектура
```
Telegram → bot.py → Claude API (tool_use) → инструменты → ответ
                         Railway (облако)
```

## Файлы
- `bot.py` — основной код: agent loop, инструменты, handlers
- `.env` — токены (не коммитить!)
- `requirements.txt` — зависимости
- `Procfile` — команда запуска для Railway (`worker: python bot.py`)
- `credentials.json` — Google OAuth2 credentials (не коммитить!)
- `auth_google.py` — одноразовый скрипт получения refresh_token

## Переменные окружения
```
TELEGRAM_TOKEN          — токен от @BotFather
ANTHROPIC_API_KEY       — ключ Anthropic API
GOOGLE_CLIENT_ID        — Google OAuth2 client ID
GOOGLE_CLIENT_SECRET    — Google OAuth2 client secret
GOOGLE_REFRESH_TOKEN    — Google OAuth2 refresh token (получен через auth_google.py)
```

## Запуск локально
```bash
pip install -r requirements.txt
python bot.py
```

## Деплой
Автоматически при `git push` в ветку master.
Railway подхватывает изменения из GitHub.

```bash
git add .
git commit -m "описание"
git push
```

## Добавление нового инструмента
1. Добавить определение в список `TOOLS` в bot.py
2. Добавить обработку в функцию `execute_tool()` в bot.py
3. Если нужны новые зависимости — добавить в requirements.txt
4. `git push` — Railway задеплоит автоматически

## Текущие инструменты
- `get_current_datetime` — текущая дата и время
- `calendar_list_events` — список событий Google Calendar
- `calendar_create_event` — создать событие
- `calendar_delete_event` — удалить событие

## Модель
`claude-sonnet-4-6` — менять в `run_agent()` в bot.py

## Временная зона
Asia/Almaty (UTC+5) — менять в SYSTEM_PROMPT и в `calendar_create_event`
