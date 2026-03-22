# tg-bot — Личный ИИ-агент в Telegram

## Что это
Telegram-бот на базе Claude API с agent loop и инструментами. Работает 24/7 на Railway.

## Архитектура
```
Telegram → bot.py → Claude API (tool_use) → инструменты → ответ
                         Railway (облако)
                         Redis (память/история/напоминания)
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
REDIS_URL               — URL Redis (Railway Redis plugin)
BRAVE_API_KEY           — Brave Search API
OPENWEATHER_API_KEY     — OpenWeatherMap API
FAL_API_KEY             — fal.ai (генерация изображений)
REDDIT_CLIENT_ID        — Reddit API (опционально, для дайджеста)
REDDIT_CLIENT_SECRET    — Reddit API (опционально, для дайджеста)
REDDIT_USER_AGENT       — Reddit API user agent (опционально, напр. tg-bot-digest/1.0)
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
- `calendar_list_events` / `calendar_create_event` / `calendar_delete_event` — Google Calendar
- `gmail_search` / `gmail_read` / `gmail_send` — Gmail (send поддерживает вложения)
- `gmail_trash` / `gmail_trash_many` / `gmail_empty_trash` / `gmail_empty_spam` — удаление писем
- `tasks_list` / `tasks_create` / `tasks_complete` / `tasks_search` — Google Tasks (списки Задачи и Идеи)
- `drive_search` / `drive_read` / `drive_create_doc` / `drive_create_sheet` / `drive_create_slides` / `drive_create_folder` / `drive_move_file` — Google Drive
- `web_search` — Brave Search
- `get_weather` — OpenWeatherMap (текущая + прогноз)
- `get_crypto_prices` — CoinGecko + ExchangeRate (крипта и фиатные пары)
- `get_token_info` — Dexscreener (по адресу контракта)
- `reminder_set` / `reminder_list` / `reminder_cancel` — напоминания (Redis)
- `generate_image` — fal.ai FLUX dev (текст → изображение)

## Отключённые / ожидают замены
- `edit_image` — img2img отключён, ждёт новой модели (nano banana 2)

## Спецфункции
- Утренний дайджест в 11:00 Almaty — погода + события + задачи (user_id=661638470)
- Конкурентный радар `/ai_agents_digest` — каждый пн в 12:00 Almaty, автоматически (user_id=661638470)
  - Источники: Brave Search + HackerNews (+ Reddit если есть ключи)
  - Тема: ИИ-агенты / ИИ-ассистенты / чат-боты / платформы без кода
  - Фильтр под профиль продукта (SaaS персональных ИИ-ботов в Telegram, B2C СНГ)
  - Метки угроз: 🔴 высокая / 🟡 средняя / 🟢 низкая
  - Функция: `send_weekly_ai_digest()` в bot.py
  - Детали настроек: memory/skill_competitive_radar.md
- Загрузка файла/фото в Drive — отправить с подписью "в drive" (поддержка альбомов и создания папки)
- Отправка письма с вложением — скинуть файл в чат + попросить отправить письмо
  - Паттерн: `_pending_attachments[user_id]` хранит последний файл из чата
  - `gmail_send` подхватывает его автоматически через MIMEMultipart
  - Работает для любых форматов: xlsx, pdf, docx, и т.д.
- Чтение PDF и Word документов
- Калории — нативно через Claude (без API)
- Анализ фото — нативно через Claude Vision

## Технические особенности
- История обрезается до 15 сообщений. При обрезке автоматически удаляются осиротевшие `tool_result` блоки в начале (иначе Claude падает с BadRequestError 400)
- Медиа-группы (альбомы): буферизация 1.5с через `_media_group_buffer` + asyncio
- Меню бота регистрируется через `app.post_init` → `set_my_commands()` при старте
- `run_weekly` отсутствует в python-telegram-bot v21 → используем `run_daily(days=(1,))` для понедельника (0=вс, 1=пн!)
- Автоматические дайджесты записываются в Redis историю после отправки — иначе бот не помнит что отправлял

## Модель
`claude-sonnet-4-6` — менять в `run_agent()` в bot.py

## Временная зона
Asia/Almaty (UTC+5) — задана через pytz в `TZ = pytz.timezone("Asia/Almaty")`
