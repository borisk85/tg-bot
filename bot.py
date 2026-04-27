import os
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
import pytz
TZ = pytz.timezone("Asia/Almaty")

def now_local():
    return datetime.now(TZ)
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

import re
import threading
import requests
import redis
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Redis для хранения истории ────────────────────────────────────────────────

redis_client = None
try:
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        redis_client = redis.from_url(redis_url, decode_responses=True)
        redis_client.ping()
        logger.info("Redis подключён")
    else:
        logger.info("REDIS_URL не задан — используем память")
except Exception as e:
    logger.warning(f"Redis недоступен, используем память: {e}")
    redis_client = None

conversations: dict[int, list] = {}

def get_history(user_id: int) -> list:
    if redis_client:
        data = redis_client.get(f"conv:{user_id}")
        return json.loads(data) if data else []
    return conversations.get(user_id, [])

def set_history(user_id: int, history: list):
    if redis_client:
        redis_client.setex(f"conv:{user_id}", 60*60*24*7, json.dumps(history, ensure_ascii=False))
    else:
        conversations[user_id] = history

def clear_history(user_id: int):
    if redis_client:
        redis_client.delete(f"conv:{user_id}")
    else:
        conversations[user_id] = []

def get_reminders(user_id: int) -> list:
    if redis_client:
        data = redis_client.get(f"reminders:{user_id}")
        return json.loads(data) if data else []
    return []

def save_reminders(user_id: int, reminders: list):
    if redis_client:
        redis_client.set(f"reminders:{user_id}", json.dumps(reminders, ensure_ascii=False))


_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _format_when_human(trigger_dt, now) -> str:
    """Человечный формат времени напоминания: «сегодня в 14:30», «завтра в 09:00», «28 апреля в 16:45»."""
    time_str = trigger_dt.strftime("%H:%M")
    day_diff = (trigger_dt.date() - now.date()).days
    if day_diff == 0:
        return f"сегодня в {time_str}"
    if day_diff == 1:
        return f"завтра в {time_str}"
    if day_diff == 2:
        return f"послезавтра в {time_str}"
    return f"{trigger_dt.day} {_MONTHS_RU[trigger_dt.month - 1]} в {time_str}"

def get_price_alerts(user_id: int) -> list:
    if redis_client:
        data = redis_client.get(f"price_alerts:{user_id}")
        return json.loads(data) if data else []
    return []

def save_price_alerts(user_id: int, alerts: list):
    if redis_client:
        redis_client.set(f"price_alerts:{user_id}", json.dumps(alerts, ensure_ascii=False))

def get_user_memory(user_id: int) -> list:
    if redis_client:
        data = redis_client.get(f"memory:{user_id}")
        return json.loads(data) if data else []
    return []

def save_user_memory(user_id: int, memories: list):
    if redis_client:
        redis_client.set(f"memory:{user_id}", json.dumps(memories, ensure_ascii=False))

# Маппинг тикеров крипты → CoinGecko ID
CRYPTO_TICKERS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "DOGE": "dogecoin", "BNB": "binancecoin", "XRP": "ripple",
    "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap",
}

def fetch_asset_price(ticker: str) -> float | None:
    """Единый прайс-чекер: крипта через CoinGecko (Binance fallback), остальное через yfinance."""
    try:
        if ticker in CRYPTO_TICKERS:
            coin_id = CRYPTO_TICKERS[ticker]
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
                headers={"Accept": "application/json"},
                timeout=10
            )
            if resp.status_code == 200:
                price = resp.json().get(coin_id, {}).get("usd")
                if price:
                    return price
            # Binance fallback при rate-limit или пустом ответе
            rb = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": f"{ticker}USDT"}, timeout=8
            )
            if rb.status_code == 200:
                return float(rb.json().get("price", 0)) or None
            return None
        else:
            import yfinance as yf
            price = yf.Ticker(ticker).fast_info.last_price
            return float(price) if price else None
    except Exception:
        return None

CITY_TZ = {
    "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
    "нячанг": "Asia/Ho_Chi_Minh",
    "хошимин": "Asia/Ho_Chi_Minh",
    "дубай": "Asia/Dubai",
    "анталья": "Europe/Istanbul",
    "стамбул": "Europe/Istanbul",
    "куала-лумпур": "Asia/Kuala_Lumpur",
}

def get_user_tz(user_id: int) -> pytz.BaseTzInfo:
    if redis_client:
        tz_name = redis_client.get(f"tz:{user_id}")
        if tz_name:
            try:
                return pytz.timezone(tz_name)
            except Exception:
                pass
    return TZ  # default: Almaty

def set_user_tz(user_id: int, tz_name: str):
    if redis_client:
        redis_client.set(f"tz:{user_id}", tz_name)

def is_morning_digest_enabled(user_id: int) -> bool:
    """Утренний дайджест включён по умолчанию. Явный 'off' в Redis отключает."""
    if redis_client:
        v = redis_client.get(f"digest_morning:{user_id}")
        if v == "off":
            return False
    return True

def set_morning_digest(user_id: int, enabled: bool):
    if redis_client:
        redis_client.set(f"digest_morning:{user_id}", "on" if enabled else "off")

def get_digest_time(user_id: int) -> tuple:
    """Возвращает (hour, minute) для утреннего дайджеста. По умолчанию 11:00."""
    if redis_client:
        v = redis_client.get(f"digest_time:{user_id}")
        if v:
            parts = v.split(":")
            return int(parts[0]), int(parts[1])
    return 11, 0

def set_digest_time(user_id: int, hour: int, minute: int):
    if redis_client:
        redis_client.set(f"digest_time:{user_id}", f"{hour}:{minute:02d}")

def serialize_messages(messages: list) -> list:
    """Конвертирует объекты Anthropic SDK в plain dict для JSON-сериализации."""
    result = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            serialized_content = []
            for block in content:
                if hasattr(block, "model_dump"):
                    serialized_content.append(block.model_dump())
                elif hasattr(block, "type"):
                    d = {"type": block.type}
                    if hasattr(block, "text"):
                        d["text"] = block.text
                    if hasattr(block, "id"):
                        d["id"] = block.id
                    if hasattr(block, "name"):
                        d["name"] = block.name
                    if hasattr(block, "input"):
                        d["input"] = block.input
                    serialized_content.append(d)
                else:
                    serialized_content.append(block)
            result.append({"role": msg["role"], "content": serialized_content})
        else:
            result.append(msg)
    return result

# ── Telegram scraper (Telethon) ───────────────────────────────────────────────

def _run_async_in_thread(coro):
    """Запускает async-корутину из синхронного кода через отдельный поток."""
    result_box = [None]
    exc_box = [None]
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_box[0] = loop.run_until_complete(coro)
        except Exception as e:
            exc_box[0] = e
        finally:
            loop.close()
    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=60)
    if exc_box[0]:
        raise exc_box[0]
    return result_box[0]

async def _fetch_tg_post(url: str) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    match = re.match(r'https?://t\.me/([^/]+)/(\d+)', url.strip())
    if not match:
        return "Неверная ссылка. Ожидается формат: https://t.me/channelname/123"

    channel = match.group(1)
    post_id = int(match.group(2))

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session_str = os.getenv("TELETHON_SESSION", "")

    if not api_id or not api_hash:
        return "Ошибка: TELEGRAM_API_ID или TELEGRAM_API_HASH не настроены в переменных окружения"

    client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
    try:
        await client.connect()

        messages = await client.get_messages(channel, ids=post_id)
        post = messages if not isinstance(messages, list) else (messages[0] if messages else None)
        if not post:
            return "Пост не найден или канал недоступен"

        post_text = post.text or post.caption or "[медиа без текста]"

        comments = []
        async for msg in client.iter_messages(channel, reply_to=post_id, limit=500):
            text = msg.text or msg.caption
            if not text:
                continue
            sender_name = "Аноним"
            if msg.sender:
                if hasattr(msg.sender, 'first_name'):
                    parts = [msg.sender.first_name or "", msg.sender.last_name or ""]
                    sender_name = " ".join(p for p in parts if p) or "Аноним"
                elif hasattr(msg.sender, 'title'):
                    sender_name = msg.sender.title or "Канал"
            comments.append(f"{sender_name}: {text}")

        result = f"ПОСТ (@{channel}/{post_id}):\n{post_text}\n\n"
        if comments:
            result += f"КОММЕНТАРИИ ({len(comments)} шт.):\n" + "\n---\n".join(comments)
        else:
            result += "Комментариев нет или комментирование закрыто."

        return result
    finally:
        await client.disconnect()

# ── Google Calendar client ────────────────────────────────────────────────────

def get_google_creds():
    return Credentials(
        token=None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/calendar",
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/tasks",
            "https://www.googleapis.com/auth/drive",
        ]
    )

def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_creds())

def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_creds())

def get_tasks_service():
    return build("tasks", "v1", credentials=get_google_creds())

def get_drive_service():
    return build("drive", "v3", credentials=get_google_creds())

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — личный ИИ-агент. Умный, краткий, полезный.
Отвечаешь на русском языке. Используй доступные инструменты когда нужно.
ВАЖНО: никогда не используй markdown: запрещены **, __, *, _, `, #, ~. Только plain text без какого-либо форматирования.
ВАЖНО: никогда не используй букву Ё/ё — только Е/е во всех словах (например "ещё" → "еще", "всё" → "все", "её" → "ее").
ВАЖНО: никогда не используй слова "алерт", "крипта" — только "уведомление" и "криптовалюта/криптовалюты" и их склонения.
Тон — профессиональный и деловой. Без панибратства, без фамильярных смайлов (😅, 😊, 😄, 🤗, 😉 и подобных).
ВАЖНО: ты — ИИ, не человек. Никогда не говори "мы" когда имеешь в виду людей или ставишь себя в один ряд с людьми. Говори "вы", "люди", "человек", "пользователи" — но не "мы". "Мы" допустимо только в контексте совместной работы с пользователем ("мы можем сделать X", "давай мы разберём").
ВАЖНО: не тащи старые темы в новые сообщения. Если разговор сменил направление — отвечай на то что сказано сейчас, не возвращайся к конкретным примерам из прошлых сообщений без явной причины. Упомянутый ранее человек, случай или пример — не повод вставлять его в каждый следующий ответ. Следи за текущим направлением разговора, а не за историей.
ВАЖНО: НИКОГДА не выдумывай контекст который не существует. Запрещено писать "именно об этом мы говорили раньше", "как мы уже обсуждали", "помнишь мы говорили" если этого разговора не было в истории диалога. Если не уверен — не добавляй отсылку к прошлому. Галлюцинация контекста хуже чем его отсутствие.
Эмодзи — только нейтральные и контекстные: погода, анализ фото, задачи, события, курсы. Максимум 1-2 на ответ. На серьёзные темы (бизнес, советы, анализ, ошибки, технические вопросы) — без эмодзи.
Правило контекста действий: когда ты выполняешь действия по просьбе пользователя (отправка письма, создание события и т.д.) — ты действуешь как агент от имени пользователя. Говори "отправил за тебя", "написал от твоего имени", "сделано" — но не присваивай себе авторство и не говори "я написал" как будто ты автор. Пользователь — автор, ты — исполнитель.
Текущая дата и время: {datetime}
При создании событий используй временную зону пользователя (указана в текущей дате/времени) если не указано другое.
Когда показываешь события — форматируй красиво, с датой и временем.
Когда пользователь называет день недели ("в среду", "в пятницу", "в понедельник") — вычисляй дату самостоятельно: берёшь ближайший такой день начиная со следующего дня. Никогда не спрашивай "какого числа?" — дата известна из текущей даты выше.

Правило: когда пользователь присылает фото с вопросом — отвечай строго по тому, что на фото. Не предлагай альтернативы, другие бренды или варианты которых нет на фото, если пользователь явно не просит "что ещё" или "альтернативы".
Правило: если пользователь присылает фото как референс (цвета, стиль, пример) и просит что-то сделать — используй информацию с фото и выполни задачу немедленно. Не описывай что на фото, не проси прислать то, что уже видно. Действуй, не анализируй вслух.
Правило: если пользователь прислал фото БЕЗ подписи — НИКОГДА не описывай его содержимое автоматически. Единственные исключения: пользователь явно написал "опиши", "что здесь", "проанализируй", "что на фото". Во всех остальных случаях: посмотри историю — если последнее действие было отправка на email, сразу отправь это фото туда же. Иначе ответь ровно одной фразой: "Фото сохранено. Что сделать — отправить на email или сохранить в Drive?" и жди команды. Описание без явного запроса — запрещено.

Правило: НИКОГДА не говори что выполнил действие, если не вызвал соответствующий инструмент. Если пользователь просит "отключи дайджест" — обязан вызвать morning_digest_toggle(enabled=false). Если просит "удали задачу" — вызови tasks_delete. Если "забудь X" — вызови memory_delete. Запрещено отвечать "готово", "отключил", "удалил" без реального вызова инструмента. Это критическая ошибка доверия.
Правило: МНОГОШАГОВЫЕ КОМАНДЫ — если пользователь просит несколько действий в одном сообщении ("удали X и создай Y", "убери одно и оставь другое") — выполни КАЖДОЕ действие отдельным вызовом инструмента. Нельзя выполнить одно и промолчать о втором. Нельзя сказать "готово" если выполнена только часть. Пример: "удали напоминание в 10 утра, оставь в 13:00" = вызов reminder_cancel для 10:00. Без вызова reminder_cancel — ЗАПРЕЩЕНО отвечать что удалил.
Правило: при извлечении текста со скриншота или фото — ВСЕГДА сохраняй эмодзи которые видны на изображении. Не пропускай их. Лимит 1-2 эмодзи на ответ НЕ распространяется на эмодзи извлеченные с фото.
Правило: никогда не упоминай и не воспроизводи URL-адреса, которые видишь на скриншотах или референс-фото. Эти ссылки — часть примера, не информация для передачи пользователю.
Правило: не придумывай ограничения инструментов — если задача выполнима (создать SVG, сохранить файл), делай без оговорок. Если данные уже есть в истории диалога — используй их, не проси снова.

Правило: если в сообщении пользователя есть [image_url:...] — это URL загруженного фото. Используй его в edit_image как image_url. КРИТИЧНО для промпта: FLUX img2img требует ПОЛНОЕ описание сцены + стиль. Сначала опиши что на фото (людей, фон, одежду), потом добавь стиль. Пример: "young Asian woman holding baby in carrier, indoor, cinematic film still, dramatic moody lighting, golden hour, 8k" — НЕ просто "cinematic style". Промпт всегда на английском.
Правило: калории считай ТОЛЬКО если пользователь явно просит ("посчитай калории", "сколько ккал", "калорийность"). Если пользователь просто присылает фото еды без запроса калорий — отвечай по контексту вопроса, не считай калории автоматически. Если попросил — отвечай кратко: название блюда и ккал, если несколько — список и итого.
Правило: для курсов валют и крипты ВСЕГДА используй get_crypto_prices, не web_search. BTC, ETH, SOL, BNB, XRP, DOGE и другие основные монеты по тикеру — ТОЛЬКО get_crypto_prices. search_token (DexScreener) — для редких/неизвестных токенов: принимает название, тикер или адрес контракта (строка 32-48 символов, может заканчиваться на "pump"). НИКОГДА не жди все адреса сразу — получил один адрес, сразу вызывай search_token. НИКОГДА не уточняй "чей это адрес" — инструмент сам вернёт название токена. НИКОГДА не используй search_token для BTC/ETH/SOL/BNB/XRP/DOGE и других монет из get_crypto_prices.
Правило: для акций, биржевых индексов (NASDAQ, S&P500, Dow Jones), драгметаллов (золото, серебро) и сырья (нефть) ВСЕГДА используй get_market_price, не web_search. Тикеры: золото=GC=F, серебро=SI=F, нефть=CL=F, NASDAQ=^IXIC, S&P500=^GSPC, Dow Jones=^DJI.
Правило: ЦЕНОВЫЕ УВЕДОМЛЕНИЯ — когда пользователь говорит "уведоми когда", "напомни когда X достигнет", "предупреди если цена упадет/вырастет до" — НЕМЕДЛЕННО вызови alert_price_set без уточняющих вопросов. Маппинг названий в тикеры: биткоин/btc→BTC, эфир/eth→ETH, солана/sol→SOL, дог/doge→DOGE, золото→GC=F, серебро→SI=F, нефть→CL=F, насдак→^IXIC, s&p500→^GSPC. direction: если цель выше текущей — "above", ниже — "below". Подтверди: "Готово 🔔 Напишу, как только [тикер] [вырастет до / упадет до] $[цена]".
Правило: для погоды ВСЕГДА используй get_weather, не web_search.
Правило: вопросы «во сколько дождь/снег», «когда начнётся/закончится дождь», «дождь утром/днём/вечером», «точное время осадков» → get_weather с hourly=true и day_offset (0=сегодня, 1=завтра, 2=послезавтра). НЕ передавай forecast_days и skip_days вместе с hourly.
Правило: формат ответа на запрос цены/курса — только строка с эмодзи + название + цена + изменение за 24ч. Без лишних полей, без комментариев, без объяснений. Пример: "📈 BTC: $85,000 (+2.3%)" или "📉 XAU/USD: $4,510 (-0.5%)". Не добавляй контекст, выводы, советы.
Правило: если спрашивают цену редкого/неизвестного токена по названию или тикеру (не из основного списка) — НЕМЕДЛЕННО вызови search_token с этим названием/тикером. Не проси пользователя уточнять — ищи сам. Только если search_token ничего не нашел — тогда попроси адрес контракта.
Правило: ФИНАНСОВЫЙ АНАЛИЗ ПО СКРИНШОТУ — если пользователь присылает скриншот с графиком (криптовалюта, акции, форекс, золото, нефть, сырье, индексы, драгметаллы, любой финансовый инструмент) и спрашивает "стоит ли покупать", "что думаешь", "проанализируй", "брать или нет" — действуй так: (1) извлеки с фото название инструмента, тикер, цену, объем, таймфрейм, паттерны на графике, (2) используй web_search для актуальных данных, (3) дай конкретный анализ: текущая цена, динамика, уровни поддержки/сопротивления, объем торгов, риски, рекомендация (покупать/продавать/ждать) с обоснованием. Не отказывайся анализировать. Не пиши "я не финансовый советник". Пользователь просит аналитику — дай аналитику с фактами и данными.
Правило: город пользователя по умолчанию — Алматы. Если спрашивают "какая погода" без указания города — используй Алматы. Другой город только если явно указан в вопросе.
Правило: НИКОГДА не используй тройные бэктики (```) в ответах — Telegram их не рендерит, они отображаются как символы. Код, SVG, JSON и любые другие блоки кода — только через drive_create_doc или как файл, но не вставляй в текст сообщения.
Правило: если пользователь просит создать SVG, HTML, код или любой текстовый файл — создай его через drive_create_doc и дай ссылку. Не вставляй содержимое файла в чат.
Правило: generate_image НЕЛЬЗЯ использовать для логотипов, иконок, SVG-фигур и любых задач где важна точная форма или цвет существующей фигуры. Для таких задач — только SVG через drive_create_doc. generate_image — исключительно для новых фото/арт изображений (люди, пейзажи, сцены).
Правило: РЕДАКТИРОВАНИЕ ИЗОБРАЖЕНИЙ — если пользователь просит изменить/подправить ранее сгенерированное изображение ("убери ноутбук", "сделай 4 пальца", "измени фон") — НЕ отказывайся. Возьми исходный промпт, внеси правки и перегенерируй через generate_image. Никогда не говори "нет инструмента для редактирования" — просто перегенерируй с обновлённым промптом. Исправляй именно то что просят — не прячь и не убирай проблемную часть.
Правило: ПОСЛЕ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЯ — ты НЕ видишь результат. НИКОГДА не описывай что "получилось" на картинке ("руки скрещены", "фон изменён", "пальцев не видно"). Ты описываешь свой промпт, а не реальную картинку — это будет враньём если результат отличается. После generate_image отвечай ТОЛЬКО: "Готово!" или "Перегенерировал!". Без описания содержимого.
Правило: ДОЛГОСРОЧНАЯ ПАМЯТЬ — автоматически сохраняй через memory_save когда пользователь называет: email-адреса ("отправь на ...", "адрес ...", "почта ..."), имена и контакты ("это мой друг Алексей", "коллега Дана"), часто используемые адреса и реквизиты, криптовалютные адреса и тикеры токенов. Для всего остального (темы обсуждений, товары, случайные интересы) — сохраняй ТОЛЬКО по явной просьбе ("запомни что...", "/about ..."). Явная просьба забыть ("забудь что...") — удаляй. При ответе используй то что уже есть в памяти — не переспрашивай то что уже знаешь.
КРИТИЧНО: если ты сам спросил "на какой адрес?" или "скинь email" — и пользователь ответил голым email-адресом — НЕМЕДЛЕННО вызови memory_save с key="email [имя из контекста разговора]", value="[адрес]" ПРЕЖДЕ чем отправлять письмо. Не жди явной просьбы "запомни" — сохраняй автоматически. Пример: спрашивал про "криптоденьги", получил "criptodengi@gmail.com" → memory_save(key="email криптоденьги", value="criptodengi@gmail.com").

Правило: ПИСЬМА — при чтении и пересказе писем ВСЕГДА показывай оригинальный текст письма как есть, без перевода. Переводи на русский ТОЛЬКО если пользователь явно попросил "переведи" или "на русском". Если письмо на английском — показывай на английском.

Правило: НИКОГДА не сохраняй, не записывай, не создавай задачи/события/напоминания если пользователь явно написал "ничего не делай", "это я для себя", "не сохраняй", "просто заметка", "для себя" — даже если в сообщении есть список дел или планы. Пользователь думает вслух или делает заметку для себя, а не просит тебя что-то сделать. В таком случае просто промолчи или ответь "Понял."

Правило: НИКОГДА не перечисляй свои возможности списком в ответе. Команда /start уже содержит полный список. Если пользователь спрашивает "ты правда это умеешь?", "что ты умеешь?" — отвечай коротко в 1-2 предложения, не пересказывай меню. Пример: "Да, все работает — просто напиши что нужно."

Правило: ОШИБКИ ИНСТРУМЕНТОВ В ИСТОРИИ — КРИТИЧНО: даже если инструмент падал 10 раз подряд в этом диалоге — ты ОБЯЗАН вызвать его снова, если пользователь просит. Ошибки временные: кредиты пополняются, серверы восстанавливаются, ключи обновляются. ЗАПРЕЩЕНО: отвечать "страница не открывается", "сервис недоступен", "технические ограничения" без реального вызова инструмента В ЭТОМ ЗАПРОСЕ. Каждый новый запрос пользователя = новая попытка. Прошлые ошибки НИКОГДА не являются причиной не вызывать инструмент.

Правило: УДАЛЕНИЕ ПИСЕМ — когда пользователь просит удалить несколько писем ("последние два", "все от X" и т.п.) — ОБЯЗАТЕЛЬНО перед удалением назови конкретные письма которые собираешься удалить ("Удалю письма от GitHub и Wolt — верно?") и дождись подтверждения. Если пользователь говорит "последние два/три" — уточни по списку из предыдущего ответа: "последние" = самые свежие по дате, не нижние в списке. Никогда не удаляй молча без подтверждения конкретного списка.
Правило: УДАЛЕНИЕ СОБЫТИЙ, ЗАДАЧ, НАПОМИНАНИЙ — если из контекста однозначно понятно что удалять (одно напоминание на сегодня, одна задача на завтра) — удаляй сразу без уточнений. Уточняй ТОЛЬКО если есть реальная неоднозначность (несколько событий в один день, несколько напоминаний). Не спрашивай подтверждение ради подтверждения.

Правило: ОТВЕТ НА ПИСЬМО — когда пользователь просит "ответь на письмо от X" или "ответь на последнее письмо от X" — СНАЧАЛА вызови gmail_search("from:X", maxResults=1), потом gmail_read чтобы прочитать содержимое и узнать тему (subject), потом gmail_send с reply_to_id. Никогда не уточняй тему письма у пользователя — найди её сам через gmail_read. Тема ответа = "Re: [оригинальная тема]".

Правило: КОДЫ ИЗ ПИСЕМ — когда пользователь просит "код из письма", "цифры из письма", "код подтверждения", "OTP", "verification code" и не указывает конкретного отправителя — немедленно вызови gmail_search с запросом "in:inbox" (или "verification OR code OR код OR подтверждение", maxResults=1), потом gmail_read на первое найденное письмо, выдай код. Не переспрашивай "от кого письмо?" — просто читай последнее входящее.

Правило: ПЕРЕХОД ПО ССЫЛКЕ ИЗ ПИСЬМА — когда пользователь просит "перейди по ссылке", "подтверди", "кликни", "нажми на кнопку подтверждения", "верифицируй", "активируй" — используй open_url. Сначала вызови gmail_read чтобы найти нужную ссылку в письме (она будет в блоке "[Ссылки из письма:]"), потом НЕМЕДЛЕННО вызови open_url с этой ссылкой. ЗАПРЕЩЕНО: говорить "не могу открывать ссылки", "требуется авторизация в браузере", "нужна активная сессия". Ссылки подтверждения в письмах содержат токен прямо в URL и работают без браузерной сессии — просто вызови open_url и посмотри на результат. Не рассуждай заранее — действуй.

Правило: Tasks — редактирование:
- Если нужно добавить текст к существующей задаче/идее — используй tasks_update с append_notes
- Если нужно удалить дубликат или ненужную задачу — используй tasks_delete
- НЕ создавай новую задачу если просят обновить или дополнить существующую

Правило: Calendar vs Tasks:
- Google Calendar — только если есть конкретная дата И время (встречи, события, звонки)
- Google Tasks, список «Задачи» — дела без времени, туду, напоминания, покупки
- Google Tasks, список «Идеи» — идеи, мысли, заметки, записать что-то на память

Правило: Google Calendar и уведомления:
- При создании события в Google Calendar НИКОГДА не предлагай и не устанавливай напоминания от себя (ни за 15 мин, ни за 30 мин, ни за сутки)
- Google Calendar сам управляет уведомлениями — это настройки пользователя, не трогай их
- После создания события просто подтверди: "Готово! [название] — [дата и время]." Без упоминания напоминаний
- reminder_set использовать ТОЛЬКО если пользователь прямо просит: "напомни мне через X", "напомни в X часов"

Контакты пользователя:
- Жена: Дана, dana.aristanbayeva@gmail.com

Правило: когда пользователь присылает URL сайта и просит прочитать, резюмировать или проанализировать — используй read_webpage. Не говори "не могу читать JS-сайты" — read_webpage справляется с React/Next.js. open_url использовать только для confirmation/verification ссылок из писем.

Правило: АВИАБИЛЕТЫ — для поиска рейсов, перелётов и авиабилетов ВСЕГДА используй search_flights, не web_search. Инструмент работает для ЛЮБЫХ маршрутов — и международных, и внутренних (например Алматы → Астана). "Туда и обратно" → один вызов с round_trip=true. Результат search_flights — выводи ДОСЛОВНО, без изменений, сокращений и пересказа.

Команды бота:
/clear — очистить историю
/myid — Telegram ID
/ai_agents_digest — запустить дайджест по личным ИИ-ассистентам в Telegram на рынке СНГ прямо сейчас (каждый пн в 12:00 приходит автоматически)"""

# ── Weather helpers ───────────────────────────────────────────────────────────

def _weather_icon(desc: str) -> str:
    d = desc.lower()
    if any(w in d for w in ("гроза", "thunderstorm")):
        return "⛈"
    if any(w in d for w in ("ливень", "сильный дождь", "heavy rain")):
        return "🌧"
    if any(w in d for w in ("дождь", "морось", "rain", "drizzle")):
        return "🌦"
    if any(w in d for w in ("метель", "вьюга", "blizzard", "снег", "snow")):
        return "🌨"
    if any(w in d for w in ("туман", "fog", "mist")):
        return "🌫"
    if any(w in d for w in ("пасмурно", "облачно", "overcast", "broken clouds")):
        return "☁️"
    if any(w in d for w in ("переменная облачность", "scattered clouds", "few clouds", "малооблачно")):
        return "⛅"
    if any(w in d for w in ("ясно", "clear")):
        return "☀️"
    return "🌡"

def _weather_tip(desc: str, temp: float, wind: float) -> str:
    d = desc.lower()
    if any(w in d for w in ("гроза", "thunderstorm")):
        return "Лучше остаться дома — гроза ⛈"
    if any(w in d for w in ("ливень", "сильный дождь", "heavy rain")):
        return "Возьми зонт, ожидается сильный дождь 🌂"
    if any(w in d for w in ("дождь", "морось", "rain", "drizzle")):
        return "Возьми зонт ☂️"
    if any(w in d for w in ("метель", "вьюга", "blizzard")):
        return "Метель — оденься тепло и будь осторожен на дорогах 🌨"
    if any(w in d for w in ("снег", "snow")):
        return "Снег — оденься теплее 🧥"
    if any(w in d for w in ("туман", "fog", "mist")):
        return "Туман — осторожно на дорогах 🌫"
    if wind >= 15:
        return "Сильный ветер — держи шляпу 💨"
    if temp <= -15:
        return "Очень холодно — одевайся максимально тепло 🧥"
    if temp <= 0:
        return "Ниже нуля — оденься теплее 🧥"
    if temp >= 35:
        return "Сильная жара — пей больше воды 🌡"
    if temp >= 28:
        return "Жарко — не забудь воду ☀️"
    return ""

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_current_datetime",
        "description": "Возвращает текущую дату и время.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "calendar_list_events",
        "description": "Показывает предстоящие события из Google Calendar. Используй когда пользователь спрашивает про расписание, события, встречи.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "На сколько дней вперёд показать события (по умолчанию 7)"
                }
            },
            "required": []
        }
    },
    {
        "name": "calendar_create_event",
        "description": "Создаёт новое событие в Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название события"},
                "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
                "time": {"type": "string", "description": "Время начала в формате HH:MM"},
                "duration_minutes": {"type": "integer", "description": "Продолжительность в минутах (по умолчанию 60)"},
                "description": {"type": "string", "description": "Описание события (необязательно)"}
            },
            "required": ["title", "date", "time"]
        }
    },
    {
        "name": "gmail_search",
        "description": "Поиск писем в Gmail. Используй когда пользователь просит найти письма, показать входящие, найти письмо от кого-то.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос (например: 'from:ivan@gmail.com', 'subject:встреча', 'is:unread')"},
                "max_results": {"type": "integer", "description": "Максимум писем (по умолчанию 5)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "gmail_read",
        "description": "Читает конкретное письмо по ID. Используй после gmail_search чтобы прочитать содержимое письма.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID письма из результатов gmail_search"}
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "gmail_send",
        "description": "Отправляет письмо. Если пользователь прислал файл и просит отправить письмо — вложение прикрепится автоматически.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Адрес получателя или несколько через запятую"},
                "subject": {"type": "string", "description": "Тема письма"},
                "body": {"type": "string", "description": "Текст письма"},
                "reply_to_id": {"type": "string", "description": "ID письма на которое отвечаем (необязательно)"}
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "gmail_trash",
        "description": "Перемещает письмо в корзину по ID. Используй когда пользователь хочет удалить письмо.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID письма из результатов gmail_search"}
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "gmail_trash_many",
        "description": "Перемещает несколько писем в корзину по результатам поиска. Используй когда нужно массово удалить письма по запросу.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail поисковый запрос — все найденные письма уйдут в корзину"},
                "max_results": {"type": "integer", "description": "Максимум писем для удаления (по умолчанию 50)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "gmail_empty_trash",
        "description": "Полностью очищает корзину Gmail (безвозвратное удаление).",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "gmail_empty_spam",
        "description": "Полностью очищает папку Спам в Gmail (безвозвратное удаление).",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "gmail_mark_spam",
        "description": "Помечает письмо как спам и перемещает его в папку Спам. Используй когда пользователь хочет пометить письмо как спам.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID письма"}
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "gmail_unsubscribe",
        "description": "Отписывается от рассылки по ID письма. Извлекает заголовок List-Unsubscribe и выполняет отписку: HTTP-запрос если ссылка, или отправляет письмо если mailto. Используй когда пользователь хочет отписаться от рассылки.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID письма рассылки"}
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "open_url",
        "description": "Открывает URL и возвращает результат. Используй когда нужно перейти по ссылке из письма: подтвердить email, верифицировать аккаунт, активировать сервис, подтвердить действие. Claude сам извлекает нужную ссылку из письма через gmail_read и передаёт сюда.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL для перехода"},
                "method": {"type": "string", "description": "HTTP метод: GET (по умолчанию) или POST", "enum": ["GET", "POST"]}
            },
            "required": ["url"]
        }
    },
    {
        "name": "web_search",
        "description": "Поиск в интернете через Brave Search. Используй когда нужна актуальная информация, новости, факты, цены, погода и т.п.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "drive_search",
        "description": "Ищет файлы и папки в Google Drive по названию или типу.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Что искать (название файла, часть названия)"},
                "file_type": {"type": "string", "description": "Тип файла: doc, sheet, pdf, folder, presentation (необязательно)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "drive_read",
        "description": "Читает содержимое текстового файла или Google Doc из Drive по ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "ID файла из drive_search"}
            },
            "required": ["file_id"]
        }
    },
    {
        "name": "generate_image",
        "description": "Генерирует изображение по текстовому описанию через FLUX. Используй ТОЛЬКО для новых художественных/фотореалистичных изображений. НЕЛЬЗЯ использовать для: логотипов, SVG, иконок, геометрических фигур, изменения цвета существующих фигур — для этого создавай SVG через drive_create_doc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Описание изображения на английском (переведи если нужно)"},
                "size": {"type": "string", "description": "Размер: square (1:1), landscape (16:9), portrait (9:16). По умолчанию square."}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "drive_create_sheet",
        "description": "Создаёт новую таблицу Google Sheets в Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название таблицы"},
                "folder_id": {"type": "string", "description": "ID папки (необязательно)"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "drive_create_slides",
        "description": "Создаёт новую презентацию Google Slides в Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название презентации"},
                "folder_id": {"type": "string", "description": "ID папки (необязательно)"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "drive_create_folder",
        "description": "Создаёт папку в Google Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Название папки"},
                "parent_id": {"type": "string", "description": "ID родительской папки (необязательно, по умолчанию корень)"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "drive_move_file",
        "description": "Перемещает файл в другую папку в Google Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "ID файла"},
                "folder_id": {"type": "string", "description": "ID папки назначения"}
            },
            "required": ["file_id", "folder_id"]
        }
    },
    {
        "name": "drive_delete",
        "description": "Удаляет файл или папку из Google Drive (перемещает в корзину).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Название файла или папки"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "drive_create_doc",
        "description": "Создаёт новый Google Doc с текстом.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название документа"},
                "content": {"type": "string", "description": "Содержимое документа"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "reminder_set",
        "description": "Устанавливает напоминание. Бот напишет пользователю в указанное время. Используй когда просят напомнить через N минут/часов или в конкретное время/дату.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст напоминания"},
                "datetime": {"type": "string", "description": "Когда напомнить — ISO формат YYYY-MM-DDTHH:MM или относительно: '+30m', '+2h', '+1d'"}
            },
            "required": ["text", "datetime"]
        }
    },
    {
        "name": "reminder_list",
        "description": "Показывает все активные напоминания пользователя.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "reminder_cancel",
        "description": "Отменяет напоминание. Три способа поиска (любой один): index (номер из reminder_list), text (часть текста), time (время срабатывания, формат HH:MM или YYYY-MM-DDTHH:MM). Пример: 'удали напоминание в 10 утра' → time='10:00'. Если сомнения — сначала reminder_list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Номер напоминания из reminder_list (начиная с 1)"},
                "text": {"type": "string", "description": "Часть текста напоминания для поиска (если не знаешь индекс)"},
                "time": {"type": "string", "description": "Время срабатывания напоминания (HH:MM или YYYY-MM-DDTHH:MM). 'удали в 10 утра' → time='10:00'"}
            }
        }
    },
    {
        "name": "memory_save",
        "description": "Сохраняет важный факт о пользователе в долгосрочную память (хранится навсегда). Используй когда пользователь сообщает: адреса контрактов, тикеры которые он следит, предпочтения, имена, города, важные числа, любые данные которые стоит помнить в следующих сессиях.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Короткое название факта, например: 'testicle_contract', 'home_city', 'btc_stack'"},
                "value": {"type": "string", "description": "Значение факта"}
            },
            "required": ["key", "value"]
        }
    },
    {
        "name": "memory_list",
        "description": "Показывает всё что сохранено в долгосрочной памяти о пользователе.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "memory_delete",
        "description": "Удаляет факт из долгосрочной памяти по ключу.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Ключ факта для удаления"}
            },
            "required": ["key"]
        }
    },
    {
        "name": "morning_digest_toggle",
        "description": "Включает или отключает утренний дайджест (погода, события, задачи). Используй когда пользователь просит 'отключи дайджест', 'не присылай больше', 'верни дайджест', 'включи обратно'. ОБЯЗАТЕЛЬНО вызови этот инструмент — нельзя просто сказать что отключил, не вызвав его.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true — включить, false — отключить"}
            },
            "required": ["enabled"]
        }
    },
    {
        "name": "morning_digest_status",
        "description": "Показывает текущий статус утреннего дайджеста (включён/отключён и время).",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "morning_digest_set_time",
        "description": "Устанавливает время утреннего дайджеста. 'присылай дайджест в 8 утра' → hour=8, minute=0.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hour": {"type": "integer", "description": "Час (0-23)"},
                "minute": {"type": "integer", "description": "Минуты (0-59), по умолчанию 0"}
            },
            "required": ["hour"]
        }
    },
    {
        "name": "alert_price_set",
        "description": "Устанавливает ценовое уведомление: напишет пользователю когда актив достигнет заданной цены. Используй для крипты (BTC, ETH, SOL, DOGE и др.), металлов (GC=F=золото, SI=F=серебро), индексов (^IXIC=NASDAQ, ^GSPC=S&P500) и акций (TSLA, AAPL и др.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Тикер актива: BTC, ETH, SOL, GC=F, ^IXIC, TSLA и т.д."},
                "target_price": {"type": "number", "description": "Целевая цена в USD"},
                "direction": {"type": "string", "enum": ["above", "below"], "description": "above — уведомить когда цена поднимется до, below — когда упадет до"}
            },
            "required": ["ticker", "target_price", "direction"]
        }
    },
    {
        "name": "alert_price_list",
        "description": "Показывает все активные ценовые уведомления пользователя.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "alert_price_cancel",
        "description": "Отменяет ценовое уведомление по номеру из alert_price_list (начиная с 1) или по тикеру.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Номер уведомления из alert_price_list (начиная с 1)"},
                "ticker": {"type": "string", "description": "Тикер актива — удалит все уведомления по этому тикеру"}
            }
        }
    },
    {
        "name": "search_token",
        "description": "Ищет токен через DexScreener по названию, тикеру или адресу контракта. ВСЕГДА вызывай когда пользователь спрашивает цену токена которого нет в get_crypto_prices — независимо от того, дал он название, тикер или адрес. Никогда не проси пользователя уточнить тикер самостоятельно — сначала попробуй найти через этот инструмент.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Название токена, тикер (IRYNA, BONK, PEPE) или адрес контракта (0x... для EVM, 32-44 символа для Solana)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_crypto_prices",
        "description": "Получает курсы криптовалют к USD и курсы любых мировых валют друг к другу. ВСЕГДА используй этот инструмент когда спрашивают про курсы валют или крипты — не используй web_search для этого.",
        "input_schema": {
            "type": "object",
            "properties": {
                "coins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список криптовалют — тикеры (BTC, ETH, SOL, BNB, XRP, DOGE...) или CoinGecko ID (bitcoin, ethereum...). Оставь пустым если нужны только фиатные валюты."
                },
                "currencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Пары фиатных валют в формате 'FROM/TO', например: 'USD/KZT', 'EUR/RUB', 'USD/EUR'. Можно несколько."
                }
            },
            "required": []
        }
    },
    {
        "name": "get_market_price",
        "description": "Получает реальную цену акций, индексов, драгоценных металлов через Yahoo Finance. Используй для: золото (XAU/USD → тикер 'GC=F'), серебро ('SI=F'), нефть ('CL=F'), S&P500 ('^GSPC'), NASDAQ ('^IXIC'), Dow Jones ('^DJI'), Tesla ('TSLA'), Apple ('AAPL') и любых других тикеров. ВСЕГДА используй этот инструмент для акций, индексов и металлов — не используй web_search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список Yahoo Finance тикеров, например: ['GC=F', '^IXIC', 'TSLA']"
                }
            },
            "required": ["tickers"]
        }
    },
    {
        "name": "get_weather",
        "description": "Получает текущую погоду и прогноз на несколько дней для любого города.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Название города (например: Алматы, Москва, London)"},
                "forecast_days": {"type": "integer", "description": "Сколько дней прогноза (1-5). НЕ ПЕРЕДАВАЙ этот параметр если пользователь не просил прогноз явно. 'погода' = только текущая, forecast_days не передавать. 'прогноз на 3 дня' = forecast_days=3."},
                "skip_days": {"type": "integer", "description": "Сколько дней пропустить от завтра. 'послезавтра' = skip_days=1, days=1. 'через 3 дня' = skip_days=2, days=1. ТОЛЬКО для forecast_days, НЕ для hourly."},
                "hourly": {"type": "boolean", "description": "Вернуть почасовую разбивку (шаг 3 часа) с временем, температурой, описанием, вероятностью осадков и мм дождя. Используй когда спрашивают 'во сколько дождь', 'когда начнётся/закончится дождь', 'дождь утром/днём/вечером'. Совмести с day_offset для выбора дня."},
                "day_offset": {"type": "integer", "description": "Для hourly: какой день — 0=сегодня (дефолт), 1=завтра, 2=послезавтра."}
            },
            "required": ["city"]
        }
    },
    {
        "name": "tasks_list",
        "description": "Показывает задачи из Google Tasks. Используй когда пользователь просит показать задачи, список дел, заметки.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tasklist": {"type": "string", "description": "Название списка задач (по умолчанию основной список)"},
                "show_completed": {"type": "boolean", "description": "Показывать выполненные задачи (по умолчанию false)"}
            },
            "required": []
        }
    },
    {
        "name": "tasks_create",
        "description": "Создаёт новую задачу в Google Tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название задачи"},
                "notes": {"type": "string", "description": "Заметка / описание задачи (необязательно)"},
                "due": {"type": "string", "description": "Срок выполнения в формате YYYY-MM-DD (необязательно)"},
                "tasklist": {"type": "string", "description": "Название списка (по умолчанию основной)"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "tasks_update",
        "description": "Обновляет существующую задачу в Google Tasks: меняет название, заметку или дописывает текст к существующей заметке. Используй когда нужно добавить информацию к уже существующей задаче/идее.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название задачи для поиска (или часть названия)"},
                "new_title": {"type": "string", "description": "Новое название задачи (необязательно)"},
                "notes": {"type": "string", "description": "Новая заметка — полностью заменит существующую (необязательно)"},
                "append_notes": {"type": "string", "description": "Текст для добавления в конец существующей заметки (необязательно)"},
                "tasklist": {"type": "string", "description": "Название списка (необязательно, ищет во всех если не указан)"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "tasks_delete",
        "description": "Удаляет задачу из Google Tasks по названию.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название задачи для удаления (или часть названия)"},
                "tasklist": {"type": "string", "description": "Название списка (необязательно)"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "tasks_delete_all",
        "description": "Удаляет ВСЕ задачи из списка Google Tasks. Используй когда пользователь просит 'очисти список', 'удали все задачи', 'очисти задачи'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tasklist": {"type": "string", "description": "Название списка (необязательно — если не указан, удаляет из всех списков)"}
            },
            "required": []
        }
    },
    {
        "name": "tasks_complete",
        "description": "Отмечает задачу как выполненную.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название задачи (или часть названия)"},
                "tasklist": {"type": "string", "description": "Название списка (необязательно)"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "tasks_search",
        "description": "Ищет задачи по тексту во всех списках.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "calendar_delete_event",
        "description": "Удаляет событие из Google Calendar по названию и дате.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название события для удаления"},
                "date": {"type": "string", "description": "Дата события YYYY-MM-DD"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "gmail_send_draft",
        "description": "Отправляет черновик из Gmail Drafts. Используй когда Boris говорит 'отправь черновик' или 'отправь' после того как черновик письма уже создан. keyword — ключевое слово из темы черновика.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Ключевое слово из темы черновика для поиска (необязательно — если черновик один, отправит его)"}
            },
            "required": []
        }
    },
    {
        "name": "read_webpage",
        "description": "Читает полное содержимое любой веб-страницы, включая React/Next.js сайты. Возвращает чистый текст/markdown. Используй когда пользователь присылает URL и просит прочитать, резюмировать, проанализировать сайт, лендинг, статью, документацию. Не использовать для confirmation-ссылок из писем — для них open_url.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL страницы для чтения"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "search_flights",
        "description": "Ищет авиабилеты через Travelpayouts/Aviasales. Используй для любых запросов о рейсах, перелётах, авиабилетах — международных и внутренних. Результат возвращать ДОСЛОВНО без изменений.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Город вылета (например: Алматы, Москва, ALA). Принимает название на русском или IATA-код."},
                "destination": {"type": "string", "description": "Город назначения (например: Дубай, Стамбул, DXB). Принимает название на русском или IATA-код."},
                "month": {"type": "string", "description": "Месяц перелёта в формате YYYY-MM (например: 2026-05)"},
                "max_price": {"type": "number", "description": "Максимальная цена в USD (необязательно)"},
                "direct_only": {"type": "boolean", "description": "Только прямые рейсы (по умолчанию false)"},
                "airline": {"type": "string", "description": "Предпочтительная авиакомпания (необязательно, например: Air Astana, Turkish Airlines, Emirates)"},
                "departure_time": {"type": "string", "description": "Время вылета: утро / день / вечер / ночь (необязательно)"},
                "max_duration_hours": {"type": "number", "description": "Максимальное время в пути в часах (необязательно)"},
                "day_from": {"type": "integer", "description": "Вылет начиная с числа месяца (необязательно, 1-31)"},
                "day_to": {"type": "integer", "description": "Вылет не позже числа месяца (необязательно, 1-31)"},
                "round_trip": {"type": "boolean", "description": "Туда-обратно — один вызов возвращает обе части (по умолчанию false)"},
                "return_month": {"type": "string", "description": "Месяц обратного рейса в формате YYYY-MM, если отличается от основного (необязательно)"}
            },
            "required": ["origin", "destination", "month"]
        }
    }
]

# ── Tool execution ────────────────────────────────────────────────────────────

async def execute_tool(name: str, tool_input: dict, user_id: int = None) -> str:
    logger.info(f"Tool: {name}({json.dumps(tool_input, ensure_ascii=False)})")

    if name == "get_current_datetime":
        now = now_local()
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return f"{now.strftime('%d.%m.%Y')}, {days[now.weekday()]}, {now.strftime('%H:%M')}"

    if name == "gmail_search":
        try:
            import base64
            service = get_gmail_service()
            query = tool_input["query"]
            max_results = tool_input.get("max_results", 5)
            result = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            messages = result.get("messages", [])
            if not messages:
                return "Писем не найдено."
            output = []
            for msg in messages:
                m = service.users().messages().get(userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in m["payload"]["headers"]}
                output.append(f"ID: {msg['id']}\nОт: {headers.get('From','?')}\nТема: {headers.get('Subject','?')}\nДата: {headers.get('Date','?')}")
            return "\n\n".join(output)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_read":
        try:
            import base64, re as _re
            service = get_gmail_service()
            msg = service.users().messages().get(userId="me", id=tool_input["message_id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

            def extract_parts(payload):
                """Рекурсивно собирает (plain_text, html_text) из любой вложенности."""
                plain, html = "", ""
                if "parts" in payload:
                    for part in payload["parts"]:
                        p, h = extract_parts(part)
                        plain = plain or p
                        html = html or h
                else:
                    data = payload.get("body", {}).get("data", "")
                    if data:
                        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                        if payload.get("mimeType") == "text/plain":
                            plain = text
                        elif payload.get("mimeType") == "text/html":
                            html = text
                return plain, html

            plain, html = extract_parts(msg["payload"])
            if plain:
                body = plain
            elif html:
                # Убираем style/script блоки целиком вместе с содержимым
                body = _re.sub(r'<style[^>]*>.*?</style>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
                body = _re.sub(r'<script[^>]*>.*?</script>', '', body, flags=_re.DOTALL | _re.IGNORECASE)
                # Извлекаем href-ссылки из <a> тегов ДО снятия тегов
                links = _re.findall(r'<a[^>]+href=["\']([^"\']{10,})["\'][^>]*>(.*?)</a>', body, _re.IGNORECASE | _re.DOTALL)
                # Извлекаем form action URLs
                form_actions = _re.findall(r'<form[^>]+action=["\']([^"\']{10,})["\']', body, _re.IGNORECASE)
                # Заменяем <br>, <p>, <tr>, <li> на переносы для читаемости
                body = _re.sub(r'<(br|p|tr|li)[^>]*>', '\n', body, flags=_re.IGNORECASE)
                # Снимаем оставшиеся теги
                body = _re.sub(r'<[^>]+>', '', body)
                # Декодируем HTML entities
                body = body.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
                body = _re.sub(r'[ \t]+', ' ', body)
                body = _re.sub(r'\n{3,}', '\n\n', body).strip()
                # Добавляем извлечённые ссылки в конец (исключаем только пиксели/картинки)
                skip_ext = ('.gif', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.woff')
                collected = []
                for url, raw_text in links:
                    if not url.startswith('http'):
                        continue
                    if any(url.lower().endswith(e) for e in skip_ext):
                        continue
                    text = _re.sub(r'<[^>]+>', '', raw_text).strip()[:60] or url[:60]
                    collected.append((text, url))
                for url in form_actions:
                    if url.startswith('http'):
                        collected.append(('(форма)', url))
                if collected:
                    body += "\n\n[Ссылки из письма:]\n" + "\n".join(f"- {text}: {url}" for text, url in collected[:15])
            else:
                body = "(текст письма не удалось извлечь)"

            return f"От: {headers.get('From','?')}\nКому: {headers.get('To','?')}\nТема: {headers.get('Subject','?')}\nДата: {headers.get('Date','?')}\n\n{body[:4000]}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_send":
        try:
            import base64
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from email.mime.base import MIMEBase
            from email import encoders
            service = get_gmail_service()

            # Проверяем есть ли вложения от пользователя (одно или список)
            raw_att = _pending_attachments.pop(user_id, None) if user_id else None
            if raw_att is None:
                attachments = []
            elif isinstance(raw_att, list):
                attachments = raw_att
            else:
                attachments = [raw_att]

            if attachments:
                msg = MIMEMultipart()
                msg.attach(MIMEText(tool_input["body"], "plain", "utf-8"))
                for att in attachments:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(att["bytes"])
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f'attachment; filename="{att["filename"]}"')
                    part.add_header("Content-Type", att["mime"])
                    msg.attach(part)
            else:
                msg = MIMEText(tool_input["body"], "plain", "utf-8")

            msg["to"] = tool_input["to"]
            msg["subject"] = tool_input["subject"]

            if tool_input.get("reply_to_id"):
                original = service.users().messages().get(userId="me", id=tool_input["reply_to_id"], format="metadata",
                    metadataHeaders=["Message-ID", "Subject"]).execute()
                headers = {h["name"]: h["value"] for h in original["payload"]["headers"]}
                msg["In-Reply-To"] = headers.get("Message-ID", "")
                msg["References"] = headers.get("Message-ID", "")

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            body = {"raw": raw}
            if tool_input.get("reply_to_id"):
                thread = service.users().messages().get(userId="me", id=tool_input["reply_to_id"], format="minimal").execute()
                body["threadId"] = thread.get("threadId")
            service.users().messages().send(userId="me", body=body).execute()
            result = f"Письмо отправлено на {tool_input['to']}."
            if attachments:
                names = ", ".join(a["filename"] for a in attachments)
                result += f" Вложения: {names}."
            return result
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_trash":
        try:
            service = get_gmail_service()
            service.users().messages().trash(userId="me", id=tool_input["message_id"]).execute()
            return "Письмо перемещено в корзину."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_mark_spam":
        try:
            service = get_gmail_service()
            service.users().messages().modify(
                userId="me",
                id=tool_input["message_id"],
                body={"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]}
            ).execute()
            return "Письмо помечено как спам и перемещено в папку Спам."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_unsubscribe":
        try:
            import re as _re, base64
            from email.mime.text import MIMEText

            def extract_body_parts(payload):
                plain, html = "", ""
                if "parts" in payload:
                    for part in payload["parts"]:
                        p, h = extract_body_parts(part)
                        plain = plain or p
                        html = html or h
                else:
                    data = payload.get("body", {}).get("data", "")
                    if data:
                        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                        if payload.get("mimeType") == "text/plain":
                            plain = text
                        elif payload.get("mimeType") == "text/html":
                            html = text
                return plain, html

            service = get_gmail_service()
            msg = service.users().messages().get(userId="me", id=tool_input["message_id"], format="full").execute()
            hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            unsub = hdrs.get("List-Unsubscribe", "")
            subject = hdrs.get("Subject", "")
            sender = hdrs.get("From", "")

            # Шаг 1: заголовок List-Unsubscribe
            urls = _re.findall(r'<([^>]+)>', unsub) if unsub else []
            http_url = next((u for u in urls if u.startswith("http")), None)
            mailto = next((u for u in urls if u.startswith("mailto:")), None)

            # Шаг 2: если заголовка нет — ищем ссылку в теле письма
            if not http_url and not mailto:
                _, html_body = extract_body_parts(msg["payload"])
                plain_body = msg.get("snippet", "")
                # Ищем href рядом со словом unsubscribe/отписаться
                body_urls = _re.findall(
                    r'href=["\']([^"\']+)["\'][^>]*>[^<]*(?:unsubscribe|отписаться|opt.?out)[^<]*<',
                    html_body, _re.IGNORECASE
                )
                if not body_urls:
                    # Обратный порядок: текст ссылки → href
                    body_urls = _re.findall(
                        r'href=["\']([^"\']{20,}unsubscri[^"\']*)["\']',
                        html_body, _re.IGNORECASE
                    )
                if body_urls:
                    http_url = body_urls[0]

            if http_url:
                resp = requests.get(http_url, timeout=15, allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code >= 400:
                    post_data = hdrs.get("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")
                    resp = requests.post(http_url, data=post_data, timeout=15,
                        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})
                return f"Отписка выполнена (HTTP {resp.status_code}). Рассылка: {subject} от {sender}."
            elif mailto:
                addr = mailto.replace("mailto:", "").split("?")[0]
                subj_match = _re.search(r'subject=([^&]+)', mailto)
                mail_subj = subj_match.group(1) if subj_match else "Unsubscribe"
                mail_msg = MIMEText("", "plain", "utf-8")
                mail_msg["To"] = addr
                mail_msg["Subject"] = mail_subj
                raw = base64.urlsafe_b64encode(mail_msg.as_bytes()).decode()
                service.users().messages().send(userId="me", body={"raw": raw}).execute()
                return f"Письмо-запрос на отписку отправлено на {addr}. Рассылка: {subject} от {sender}."
            else:
                return f"Ссылка для отписки не найдена ни в заголовках, ни в теле письма. Рассылка: {subject} от {sender}. Придется открыть письмо вручную."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "open_url":
        try:
            import re as _re
            url = tool_input["url"]
            method = tool_input.get("method", "GET").upper()
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            if method == "POST":
                resp = requests.post(url, timeout=15, allow_redirects=True, headers=headers)
            else:
                resp = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
            # Извлечь заголовок страницы из HTML
            title_match = _re.search(r'<title[^>]*>([^<]{1,200})</title>', resp.text, _re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else ""
            # Краткий текст страницы (без тегов)
            text = _re.sub(r'<[^>]+>', ' ', resp.text)
            text = _re.sub(r'\s+', ' ', text).strip()[:300]
            result = f"HTTP {resp.status_code} | URL: {resp.url}"
            if title:
                result += f"\nЗаголовок: {title}"
            if text:
                result += f"\nСодержимое: {text}"
            return result
        except Exception as e:
            return f"Ошибка при открытии URL: {e}"

    if name == "gmail_trash_many":
        try:
            service = get_gmail_service()
            query = tool_input["query"]
            max_results = tool_input.get("max_results", 50)
            result = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            messages = result.get("messages", [])
            if not messages:
                return "Писем по запросу не найдено."
            for msg in messages:
                service.users().messages().trash(userId="me", id=msg["id"]).execute()
            return f"Перемещено в корзину: {len(messages)} писем."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_empty_trash":
        try:
            service = get_gmail_service()
            service.users().messages().batchDelete(userId="me", body={"ids": []})
            # Используем правильный метод — emptyTrash если доступен, иначе через список
            # Gmail API не имеет emptyTrash, удаляем через поиск in:trash
            result = service.users().messages().list(userId="me", q="in:trash", maxResults=500).execute()
            messages = result.get("messages", [])
            if not messages:
                return "Корзина уже пуста."
            ids = [m["id"] for m in messages]
            service.users().messages().batchDelete(userId="me", body={"ids": ids}).execute()
            return f"Корзина очищена: удалено {len(ids)} писем."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_empty_spam":
        try:
            service = get_gmail_service()
            result = service.users().messages().list(userId="me", q="in:spam", maxResults=500).execute()
            messages = result.get("messages", [])
            if not messages:
                return "Спам уже пуст."
            ids = [m["id"] for m in messages]
            service.users().messages().batchDelete(userId="me", body={"ids": ids}).execute()
            return f"Спам очищен: удалено {len(ids)} писем."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_search":
        try:
            service = get_drive_service()
            q_parts = [f"name contains '{tool_input['query']}'", "trashed = false"]
            type_map = {
                "doc": "application/vnd.google-apps.document",
                "sheet": "application/vnd.google-apps.spreadsheet",
                "presentation": "application/vnd.google-apps.presentation",
                "folder": "application/vnd.google-apps.folder",
                "pdf": "application/pdf",
            }
            if tool_input.get("file_type") and tool_input["file_type"] in type_map:
                q_parts.append(f"mimeType = '{type_map[tool_input['file_type']]}'")
            results = service.files().list(
                q=" and ".join(q_parts),
                fields="files(id, name, mimeType, modifiedTime, size)",
                orderBy="modifiedTime desc",
                pageSize=10
            ).execute()
            files = results.get("files", [])
            if not files:
                return "Файлы не найдены."
            lines = []
            for f in files:
                mt = f.get("modifiedTime", "")[:10]
                lines.append(f"ID: {f['id']}\n{f['name']} ({mt})")
            return "\n\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "edit_image":
        try:
            import fal_client
            os.environ["FAL_KEY"] = os.getenv("FAL_API_KEY", "")
            result = fal_client.run(
                "fal-ai/flux/dev/image-to-image",
                arguments={
                    "prompt": tool_input["prompt"],
                    "image_url": tool_input["image_url"],
                    "strength": tool_input.get("strength", 0.92),
                    "num_inference_steps": 28,
                    "num_images": 1
                }
            )
            images = result.get("images", [])
            if not images:
                return "Не удалось изменить изображение."
            return f"IMAGE_URL:{images[0]['url']}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "generate_image":
        try:
            import fal_client
            os.environ["FAL_KEY"] = os.getenv("FAL_API_KEY", "")
            prompt = tool_input["prompt"]
            size = tool_input.get("size", "square")
            size_map = {"square": "square_hd", "landscape": "landscape_16_9", "portrait": "portrait_9_16"}
            result = fal_client.run(
                "fal-ai/flux/dev",
                arguments={"prompt": prompt, "image_size": size_map.get(size, "square_hd"), "num_inference_steps": 28, "guidance_scale": 3.5, "num_images": 1}
            )
            images = result.get("images", [])
            if not images:
                return "Не удалось сгенерировать изображение."
            return f"IMAGE_URL:{images[0]['url']}"
        except Exception as e:
            return f"Ошибка генерации: {e}"

    if name == "drive_create_sheet":
        try:
            service = get_drive_service()
            meta = {"name": tool_input["title"], "mimeType": "application/vnd.google-apps.spreadsheet"}
            if tool_input.get("folder_id"):
                meta["parents"] = [tool_input["folder_id"]]
            f = service.files().create(body=meta, fields="id, name").execute()
            return f"Таблица создана: {f['name']} (ID: {f['id']})"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_create_slides":
        try:
            service = get_drive_service()
            meta = {"name": tool_input["title"], "mimeType": "application/vnd.google-apps.presentation"}
            if tool_input.get("folder_id"):
                meta["parents"] = [tool_input["folder_id"]]
            f = service.files().create(body=meta, fields="id, name").execute()
            return f"Презентация создана: {f['name']} (ID: {f['id']})"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_create_folder":
        try:
            service = get_drive_service()
            meta = {"name": tool_input["name"], "mimeType": "application/vnd.google-apps.folder"}
            if tool_input.get("parent_id"):
                meta["parents"] = [tool_input["parent_id"]]
            f = service.files().create(body=meta, fields="id, name").execute()
            return f"Папка создана: {f['name']} (ID: {f['id']})"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_move_file":
        try:
            service = get_drive_service()
            file_id = tool_input["file_id"]
            folder_id = tool_input["folder_id"]
            f = service.files().get(fileId=file_id, fields="parents").execute()
            prev_parents = ",".join(f.get("parents", []))
            service.files().update(
                fileId=file_id,
                addParents=folder_id,
                removeParents=prev_parents,
                fields="id, name"
            ).execute()
            return "Файл перемещён."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_delete":
        try:
            service = get_drive_service()
            results = service.files().list(
                q=f"name contains '{tool_input['query']}' and trashed = false",
                fields="files(id, name)", pageSize=1
            ).execute()
            files = results.get("files", [])
            if not files:
                return f"Файл «{tool_input['query']}» не найден."
            f = files[0]
            service.files().update(fileId=f["id"], body={"trashed": True}).execute()
            return f"Удалено в корзину: «{f['name']}»"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_read":
        try:
            service = get_drive_service()
            file_id = tool_input["file_id"]
            meta = service.files().get(fileId=file_id, fields="mimeType, name").execute()
            mime = meta["mimeType"]

            if mime == "application/vnd.google-apps.document":
                content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
                return content.decode("utf-8")[:4000]
            elif mime == "text/plain":
                content = service.files().get_media(fileId=file_id).execute()
                return content.decode("utf-8")[:4000]
            else:
                return f"Файл '{meta['name']}' нельзя прочитать как текст (тип: {mime})"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "drive_create_doc":
        try:
            from googleapiclient.http import MediaInMemoryUpload
            service = get_drive_service()
            title = tool_input["title"]
            content = tool_input.get("content", "")
            file_meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
            media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
            f = service.files().create(body=file_meta, media_body=media, fields="id, name").execute()
            return f"Документ создан: {f['name']} (ID: {f['id']})"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "reminder_set":
        try:
            text = tool_input["text"]
            dt_str = tool_input["datetime"]
            user_tz = get_user_tz(user_id)
            now = datetime.now(user_tz)

            if dt_str.startswith("+"):
                match = re.match(r'\+(\d+)([mhd])', dt_str)
                if match:
                    val, unit = int(match.group(1)), match.group(2)
                    from datetime import timedelta
                    delta = {"m": timedelta(minutes=val), "h": timedelta(hours=val), "d": timedelta(days=val)}[unit]
                    remind_at = now + delta
                else:
                    return "Неверный формат времени. Используй +30m, +2h, +1d или YYYY-MM-DDTHH:MM"
            else:
                remind_at = user_tz.localize(datetime.fromisoformat(dt_str))

            reminders = get_reminders(user_id)
            reminders.append({"text": text, "at": remind_at.isoformat(), "done": False})
            save_reminders(user_id, reminders)
            return f"Напоминание установлено на {remind_at.strftime('%d.%m.%Y %H:%M')}: {text}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "reminder_list":
        try:
            reminders = get_reminders(user_id)
            active = [(i, r) for i, r in enumerate(reminders) if not r.get("done")]
            if not active:
                return "Активных напоминаний нет."
            user_tz = get_user_tz(user_id)
            now = datetime.now(user_tz)
            lines = []
            for idx, (_, r) in enumerate(active, 1):
                dt = datetime.fromisoformat(r["at"]).astimezone(user_tz)
                lines.append(f"{idx}. {r['text']} — {_format_when_human(dt, now)}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "reminder_cancel":
        try:
            reminders = get_reminders(user_id)
            active = [(i, r) for i, r in enumerate(reminders) if not r.get("done")]
            if not active:
                return "Нет активных напоминаний."
            real_idx, r = None, None
            if "time" in tool_input and tool_input["time"]:
                search_time = tool_input["time"]
                user_tz = get_user_tz(user_id)
                for i, rem in active:
                    rem_dt = datetime.fromisoformat(rem["at"]).astimezone(user_tz)
                    rem_hm = rem_dt.strftime("%H:%M")
                    rem_full = rem_dt.strftime("%Y-%m-%dT%H:%M")
                    if search_time in (rem_hm, rem_full):
                        real_idx, r = i, rem
                        break
                if real_idx is None:
                    return f"Напоминание на время '{search_time}' не найдено."
            elif "text" in tool_input and tool_input["text"]:
                search = tool_input["text"].lower()
                for i, rem in active:
                    if search in rem["text"].lower():
                        real_idx, r = i, rem
                        break
                if real_idx is None:
                    return f"Напоминание с текстом '{tool_input['text']}' не найдено."
            elif "index" in tool_input:
                idx = tool_input["index"] - 1
                if idx < 0 or idx >= len(active):
                    return "Напоминание не найдено."
                real_idx, r = active[idx]
            else:
                return "Укажи index или text для отмены."
            reminders[real_idx]["done"] = True
            save_reminders(user_id, reminders)
            return f"Напоминание отменено: {r['text']}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "youtube_summary":
        try:
            url = tool_input["url"]
            match = re.search(r"(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})", url)
            if not match:
                return "Не удалось извлечь ID видео из ссылки."
            video_id = match.group(1)

            from youtube_transcript_api import YouTubeTranscriptApi
            transcript = None
            transcript_error = ""
            for langs in [["ru"], ["en"], None]:
                try:
                    if langs:
                        transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
                    else:
                        transcript = YouTubeTranscriptApi.get_transcript(video_id)
                    break
                except Exception as te:
                    transcript_error = str(te)
                    continue

            if transcript:
                text = " ".join(t["text"] for t in transcript)[:8000]
                return f"TRANSCRIPT:{text}"

            # Получаем название через oEmbed
            title = ""
            try:
                oembed = requests.get(f"https://www.youtube.com/oembed?url={url}&format=json", timeout=5)
                title = oembed.json().get("title", "")
            except:
                pass

            # Ищем через Brave по названию видео
            search_q = f'"{title}" youtube' if title else f"youtube.com/watch?v={video_id}"
            try:
                headers = {"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": os.getenv("BRAVE_API_KEY")}
                resp = requests.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params={"q": search_q, "count": 5}, timeout=8)
                results = resp.json().get("web", {}).get("results", [])
                if results:
                    snippets = "\n\n".join(f"{r['title']}\n{r.get('description','')}" for r in results[:4])
                    return f"TRANSCRIPT:Субтитры недоступны. Найдено через поиск по названию '{title}':\n\n{snippets}"
            except:
                pass

            return f"Субтитры недоступны. Название: {title or 'неизвестно'}. URL: {url}"
        except Exception as e:
            return f"Не удалось получить транскрипт: {e}"

    if name == "memory_save":
        try:
            key = tool_input["key"].strip().lower().replace(" ", "_")
            value = tool_input["value"].strip()
            memories = get_user_memory(user_id)
            # Обновляем если ключ уже есть
            for m in memories:
                if m["key"] == key:
                    m["value"] = value
                    m["updated_at"] = now_local().isoformat()
                    save_user_memory(user_id, memories)
                    return f"Обновил в памяти: {key} = {value}"
            memories.append({"key": key, "value": value, "saved_at": now_local().isoformat()})
            save_user_memory(user_id, memories)
            return f"Запомнил: {key} = {value}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "memory_list":
        memories = get_user_memory(user_id)
        if not memories:
            return "Долгосрочная память пуста."
        lines = [f"✅ {m['key']}: {m['value']}" for m in memories]
        return "Что я о тебе знаю:\n" + "\n".join(lines)

    if name == "memory_delete":
        key = tool_input["key"].strip().lower().replace(" ", "_")
        memories = get_user_memory(user_id)
        new_memories = [m for m in memories if m["key"] != key]
        if len(new_memories) == len(memories):
            return f"Ключ '{key}' не найден в памяти."
        save_user_memory(user_id, new_memories)
        return f"Удалил из памяти: {key}"

    if name == "morning_digest_toggle":
        enabled = bool(tool_input.get("enabled"))
        set_morning_digest(user_id, enabled)
        h, m = get_digest_time(user_id)
        return f"Утренний дайджест включён ({h}:{m:02d})." if enabled else "Утренний дайджест отключён."

    if name == "morning_digest_status":
        h, m = get_digest_time(user_id)
        return f"Утренний дайджест: включён ({h}:{m:02d})." if is_morning_digest_enabled(user_id) else "Утренний дайджест: отключён."

    if name == "morning_digest_set_time":
        hour = tool_input["hour"]
        minute = tool_input.get("minute", 0)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return "Некорректное время."
        set_digest_time(user_id, hour, minute)
        return f"Время дайджеста изменено на {hour}:{minute:02d}."

    if name == "alert_price_set":
        try:
            ticker = tool_input["ticker"].upper()
            target = float(tool_input["target_price"])
            direction = tool_input["direction"]
            alerts = get_price_alerts(user_id)
            alerts.append({
                "ticker": ticker,
                "target_price": target,
                "direction": direction,
                "created_at": now_local().isoformat()
            })
            save_price_alerts(user_id, alerts)
            direction_text = "вырастет до" if direction == "above" else "упадет до"
            return f"Уведомление установлено: напишу когда {ticker} {direction_text} ${target:,.2f}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "alert_price_list":
        alerts = get_price_alerts(user_id)
        if not alerts:
            return "Нет активных ценовых уведомлений."
        lines = []
        for i, a in enumerate(alerts, 1):
            direction_text = "вырастет до" if a["direction"] == "above" else "упадет до"
            lines.append(f"{i}. {a['ticker']} {direction_text} ${a['target_price']:,.2f}")
        return "\n".join(lines)

    if name == "alert_price_cancel":
        alerts = get_price_alerts(user_id)
        if not alerts:
            return "Нет активных уведомлений."
        if "index" in tool_input:
            idx = tool_input["index"] - 1
            if idx < 0 or idx >= len(alerts):
                return "Уведомление не найдено."
            removed = alerts.pop(idx)
            save_price_alerts(user_id, alerts)
            return f"Уведомление на {removed['ticker']} отменено."
        elif "ticker" in tool_input:
            ticker = tool_input["ticker"].upper()
            new_alerts = [a for a in alerts if a["ticker"] != ticker]
            if len(new_alerts) == len(alerts):
                return f"Уведомлений на {ticker} не найдено."
            save_price_alerts(user_id, new_alerts)
            return f"Уведомления на {ticker} отменены."
        return "Укажи index или ticker."

    if name in ("get_token_info", "search_token"):
        try:
            query = (tool_input.get("query") or tool_input.get("address", "")).strip()
            is_contract = bool(
                re.match(r"^0x[0-9a-fA-F]{40}$", query) or
                re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,48}$", query)  # 32-48: Solana адреса включая pump.fun суффикс
            )
            if is_contract:
                resp = requests.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{query}",
                    headers={"Accept": "application/json"}, timeout=10
                )
            else:
                resp = requests.get(
                    "https://api.dexscreener.com/latest/dex/search",
                    params={"q": query},
                    headers={"Accept": "application/json"}, timeout=10
                )
            if resp.status_code != 200:
                return f"DexScreener вернул ошибку {resp.status_code}. Попробуй позже."
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return f"Токен {query!r} не найден на DexScreener. Попроси пользователя уточнить адрес контракта."
            # Сортируем по ликвидности — берем самый ликвидный
            pairs = sorted(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
            p = pairs[0]
            name_ = p.get("baseToken", {}).get("name", "?")
            symbol = p.get("baseToken", {}).get("symbol", "?")
            price = p.get("priceUsd") or "?"
            change_24h = float((p.get("priceChange") or {}).get("h24") or 0)
            chain = p.get("chainId", "").capitalize()
            dex = p.get("dexId", "").replace("-", " ").title()
            vol = float((p.get("volume") or {}).get("h24") or 0)
            arrow = "📈" if change_24h >= 0 else "📉"
            result = f"{arrow} {name_} ({symbol}): ${price}\n24h: {change_24h:+.1f}%"
            if vol:
                result += f" | Vol: ${vol:,.0f}"
            if chain:
                result += f"\n{chain}"
                if dex:
                    result += f" · {dex}"
            # Если по имени/тикеру нашлось несколько разных токенов — предупреждаем
            if not is_contract:
                same_sym = [x for x in pairs[1:6]
                            if x.get("baseToken", {}).get("symbol", "").upper() == symbol.upper()]
                if same_sym:
                    result += f"\n⚠️ Несколько токенов с тикером {symbol} — показан с наибольшей ликвидностью. Для точного поиска укажи адрес контракта."
            return result
        except Exception as e:
            return f"Ошибка DexScreener: {e}"

    if name == "get_crypto_prices":
        try:
            coins = tool_input.get("coins", [])
            currencies = tool_input.get("currencies", [])
            lines = []

            # Крипта → USD
            if coins:
                # Нормализуем входные данные: тикер BTC → CoinGecko ID bitcoin
                ticker_to_display = {}  # cg_id → display тикер
                normalized = []
                tickers_upper = []  # параллельный список верхнего регистра для Binance
                for c in coins:
                    c_upper = c.upper()
                    tickers_upper.append(c_upper)
                    if c_upper in CRYPTO_TICKERS:
                        cg_id = CRYPTO_TICKERS[c_upper]
                        ticker_to_display[cg_id] = c_upper
                        normalized.append(cg_id)
                    else:
                        ticker_to_display[c.lower()] = c_upper
                        normalized.append(c.lower())

                def fetch_via_coingecko():
                    ids = ",".join(normalized)
                    r = requests.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
                        headers={"Accept": "application/json"},
                        timeout=10
                    )
                    if r.status_code == 429:
                        return None
                    return r.json()

                def fetch_via_binance(ticker_list):
                    """Binance: публичный API без ключа, запрос по одному тикеру."""
                    result = {}
                    for t in ticker_list:
                        symbol = f"{t}USDT"
                        try:
                            r24 = requests.get(
                                "https://api.binance.com/api/v3/ticker/24hr",
                                params={"symbol": symbol}, timeout=8
                            )
                            if r24.status_code == 200:
                                d = r24.json()
                                result[t] = {
                                    "usd": float(d["lastPrice"]),
                                    "usd_24h_change": float(d["priceChangePercent"])
                                }
                        except Exception:
                            pass
                    return result

                cg_data = fetch_via_coingecko()
                if cg_data is None:
                    # CoinGecko rate-limited → Binance fallback
                    binance_data = fetch_via_binance(tickers_upper)
                    for t in tickers_upper:
                        if t in binance_data:
                            price = binance_data[t]["usd"]
                            change = binance_data[t]["usd_24h_change"]
                            arrow = "📈" if change >= 0 else "📉"
                            lines.append(f"{arrow} {t}: ${price:,.2f} ({change:+.1f}%)")
                        else:
                            lines.append(f"❓ {t}: не найдено")
                else:
                    for cg_id in normalized:
                        if cg_id in cg_data:
                            price = cg_data[cg_id]["usd"]
                            change = cg_data[cg_id].get("usd_24h_change", 0)
                            arrow = "📈" if change >= 0 else "📉"
                            symbol = ticker_to_display.get(cg_id, cg_id.upper())
                            lines.append(f"{arrow} {symbol}: ${price:,.2f} ({change:+.1f}%)")
                        else:
                            lines.append(f"❓ {ticker_to_display.get(cg_id, cg_id)}: не найдено")

            # Фиатные пары: FROM/TO
            if currencies:
                # Собираем уникальные базовые валюты
                bases = set()
                pairs = []
                for c in currencies:
                    parts = c.upper().replace("-", "/").split("/")
                    if len(parts) == 2:
                        bases.add(parts[0])
                        pairs.append((parts[0], parts[1]))

                rates_cache = {}
                for base in bases:
                    r = requests.get(f"https://api.exchangerate-api.com/v4/latest/{base}")
                    rates_cache[base] = r.json().get("rates", {})

                for frm, to in pairs:
                    rate = rates_cache.get(frm, {}).get(to)
                    if rate:
                        lines.append(f"💱 {frm}/{to}: {rate:,.4f}")
                    else:
                        lines.append(f"❓ {frm}/{to}: не найдено")

            # Если ничего не запросили — показать дефолт
            if not coins and not currencies:
                resp = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin,solana,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"},
                    headers={"Accept": "application/json"}
                )
                data = resp.json()
                for coin, sym in [("bitcoin","BTC"),("solana","SOL"),("ethereum","ETH")]:
                    price = data[coin]["usd"]
                    change = data[coin].get("usd_24h_change", 0)
                    arrow = "📈" if change >= 0 else "📉"
                    lines.append(f"{arrow} {sym}: ${price:,.2f} ({change:+.1f}%)")
                r = requests.get("https://api.exchangerate-api.com/v4/latest/USD")
                kzt = r.json().get("rates", {}).get("KZT", 0)
                if kzt:
                    lines.append(f"💱 USD/KZT: {kzt:,.0f} ₸")

            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "get_market_price":
        try:
            import yfinance as yf
            tickers = tool_input["tickers"]
            lines = []
            for ticker in tickers:
                t = yf.Ticker(ticker)
                info = t.fast_info
                price = info.last_price
                prev_close = info.previous_close
                if price is None or prev_close is None:
                    lines.append(f"❓ {ticker}: нет данных")
                    continue
                change_pct = (price - prev_close) / prev_close * 100
                arrow = "📈" if change_pct >= 0 else "📉"
                lines.append(f"{arrow} {ticker}: {price:,.2f} ({change_pct:+.1f}%)")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "get_weather":
        try:
            city = tool_input["city"]
            api_key = os.getenv("OPENWEATHER_API_KEY")
            forecast_days = tool_input.get("forecast_days", 0)
            skip_days = tool_input.get("skip_days", 0)
            hourly = tool_input.get("hourly", False)
            day_offset = tool_input.get("day_offset", 0)

            base_params = {"q": city, "appid": api_key, "units": "metric", "lang": "ru"}

            # Текущая погода (нужна всегда — для города и для fallback)
            resp = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params=base_params, timeout=10
            )
            if resp.status_code == 404:
                return f"Город «{city}» не найден."
            if resp.status_code != 200:
                return "Ошибка получения погоды."

            data = resp.json()
            city_name = data.get("name", city)
            desc = data["weather"][0]["description"].capitalize()
            temp = data["main"]["temp"]
            feels = data["main"]["feels_like"]
            humidity = data["main"]["humidity"]
            wind = data["wind"]["speed"]
            icon = _weather_icon(desc)

            if hourly:
                lat = data["coord"]["lat"]
                lon = data["coord"]["lon"]
                target_date = (now_local() + timedelta(days=day_offset)).strftime("%Y-%m-%d")
                resp2 = requests.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": lat, "longitude": lon,
                        "hourly": "precipitation_probability,precipitation",
                        "timezone": "Asia/Almaty",
                        "start_date": target_date, "end_date": target_date,
                    }, timeout=10
                )
                h = resp2.json().get("hourly", {})
                times = h.get("time", [])
                probs = h.get("precipitation_probability", [])
                precips = h.get("precipitation", [])
                if not times:
                    return f"Нет данных на {target_date} для {city_name}."

                now_hour = now_local().hour if day_offset == 0 else 0
                slots = []
                for i, t in enumerate(times):
                    hour = int(t[11:13])
                    if hour < now_hour:
                        continue
                    slots.append({"time": t[11:16], "pop": probs[i], "rain_mm": precips[i]})
                if not slots:
                    return f"Нет данных на {target_date} для {city_name}."

                threshold = 50
                day_word = "сегодня" if day_offset == 0 else ("завтра" if day_offset == 1 else f"на {target_date}")
                rainy = [s for s in slots if s["pop"] >= threshold]
                if not rainy:
                    max_pop = max(s["pop"] for s in slots)
                    if max_pop < 20:
                        return f"☀️ {day_word} дождя не ожидается."
                    return f"🌤 {day_word} дождя не ожидается (макс. вероятность {max_pop}%)."

                first = rainy[0]
                first_idx = slots.index(first)
                if first_idx == 0:
                    after_rain = next((s for s in slots[first_idx:] if s["pop"] < threshold), None)
                    if after_rain:
                        return f"🌧 Дождь идёт, закончится около {after_rain['time']}."
                    return f"🌧 Дождь идёт и продлится до конца дня."
                return f"🌧 Дождь начнётся около {first['time']}, до этого сухо."

            if forecast_days and forecast_days > 0:
                # Прогноз — показываем ТОЛЬКО прогноз без текущей погоды
                resp2 = requests.get(
                    "https://api.openweathermap.org/data/2.5/forecast",
                    params={**base_params, "cnt": 40}, timeout=10
                )
                today = now_local().strftime("%Y-%m-%d")
                day_data = {}
                for item in resp2.json().get("list", []):
                    d = item["dt_txt"][:10]
                    hour = int(item["dt_txt"][11:13])
                    if hour < 7 or hour > 21:
                        continue
                    t = item["main"]["temp"]
                    desc2 = item["weather"][0]["description"]
                    pop = item.get("pop", 0)
                    if d not in day_data:
                        day_data[d] = {"temps": [], "desc": desc2, "pop": pop}
                    day_data[d]["temps"].append(t)
                    if pop > day_data[d]["pop"]:
                        day_data[d]["pop"] = pop
                        day_data[d]["desc"] = desc2
                sorted_days = sorted(day_data.items())
                if skip_days and skip_days > 0:
                    sorted_days = sorted_days[skip_days:]
                forecast_lines = []
                for d, info in sorted_days[:forecast_days]:
                    t_min = min(info["temps"])
                    t_max = max(info["temps"])
                    icon2 = _weather_icon(info["desc"])
                    forecast_lines.append(f"{d}: {icon2} {t_min:.0f}–{t_max:.0f}°C, {info['desc']}")
                if forecast_lines:
                    return f"Прогноз для {city_name}:\n\n" + "\n".join(forecast_lines)
                return f"Не удалось получить прогноз для {city_name}."

            # Текущая погода (forecast_days == 0)
            result = (
                f"{icon} {city_name}\n"
                f"{temp:.0f}°C, ощущается {feels:.0f}°C\n"
                f"{desc}\n"
                f"Влажность {humidity}%, ветер {wind:.0f} м/с"
            )
            tip = _weather_tip(desc, temp, wind)
            if tip:
                result += f"\n{tip}"
            return result
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_list":
        try:
            service = get_tasks_service()
            tasklist_name = tool_input.get("tasklist")
            show_completed = tool_input.get("show_completed", False)

            # Найти нужный список или взять первый
            lists = service.tasklists().list().execute().get("items", [])
            if not lists:
                return "Списков задач не найдено."
            tasklist_id = lists[0]["id"]
            tasklist_title = lists[0]["title"]
            if tasklist_name:
                for tl in lists:
                    if tasklist_name.lower() in tl["title"].lower():
                        tasklist_id = tl["id"]
                        tasklist_title = tl["title"]
                        break

            params = {"tasklist": tasklist_id, "showHidden": show_completed}
            if show_completed:
                params["showCompleted"] = True
            tasks = service.tasks().list(**params).execute().get("items", [])
            if not tasks:
                return f"В списке «{tasklist_title}» нет задач."

            result = [f"📋 {tasklist_title}:"]
            for t in tasks:
                status = "✅" if t.get("status") == "completed" else "⬜"
                due = f" (до {t['due'][:10]})" if t.get("due") else ""
                notes = f"\n   {t['notes']}" if t.get("notes") else ""
                result.append(f"{status} {t['title']}{due}{notes}")
            return "\n".join(result)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_create":
        try:
            service = get_tasks_service()
            tasklist_name = tool_input.get("tasklist")
            lists = service.tasklists().list().execute().get("items", [])
            tasklist_id = lists[0]["id"] if lists else "@default"
            if tasklist_name and lists:
                for tl in lists:
                    if tasklist_name.lower() in tl["title"].lower():
                        tasklist_id = tl["id"]
                        break

            task = {"title": tool_input["title"]}
            if tool_input.get("notes"):
                task["notes"] = tool_input["notes"]
            if tool_input.get("due"):
                task["due"] = f"{tool_input['due']}T00:00:00.000Z"

            created = service.tasks().insert(tasklist=tasklist_id, body=task).execute()
            return f"Задача создана: «{created['title']}»"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_complete":
        try:
            service = get_tasks_service()
            query = tool_input["title"].lower()
            lists = service.tasklists().list().execute().get("items", [])
            for tl in lists:
                tasks = service.tasks().list(tasklist=tl["id"]).execute().get("items", [])
                for t in tasks:
                    if query in t["title"].lower() and t.get("status") != "completed":
                        t["status"] = "completed"
                        service.tasks().update(tasklist=tl["id"], task=t["id"], body=t).execute()
                        return f"Выполнено: «{t['title']}»"
            return f"Задача «{tool_input['title']}» не найдена."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_search":
        try:
            service = get_tasks_service()
            query = tool_input["query"].lower()
            lists = service.tasklists().list().execute().get("items", [])
            found = []
            for tl in lists:
                tasks = service.tasks().list(tasklist=tl["id"], showHidden=True, showCompleted=True).execute().get("items", [])
                for t in tasks:
                    if query in t["title"].lower() or query in t.get("notes", "").lower():
                        status = "✅" if t.get("status") == "completed" else "⬜"
                        found.append(f"{status} [{tl['title']}] {t['title']}")
            return "\n".join(found) if found else "Ничего не найдено."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_update":
        try:
            service = get_tasks_service()
            query = tool_input["title"].lower()
            lists = service.tasklists().list().execute().get("items", [])
            tasklist_name = tool_input.get("tasklist", "").lower()
            for tl in lists:
                if tasklist_name and tasklist_name not in tl["title"].lower():
                    continue
                tasks = service.tasks().list(tasklist=tl["id"]).execute().get("items", [])
                for t in tasks:
                    if query in t["title"].lower():
                        if tool_input.get("new_title"):
                            t["title"] = tool_input["new_title"]
                        if tool_input.get("notes") is not None:
                            t["notes"] = tool_input["notes"]
                        if tool_input.get("append_notes"):
                            existing = t.get("notes", "")
                            t["notes"] = (existing + "\n\n" + tool_input["append_notes"]).strip()
                        service.tasks().update(tasklist=tl["id"], task=t["id"], body=t).execute()
                        return f"Обновлено: «{t['title']}»"
            return f"Задача «{tool_input['title']}» не найдена."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_delete":
        try:
            service = get_tasks_service()
            query = tool_input["title"].lower()
            lists = service.tasklists().list().execute().get("items", [])
            tasklist_name = tool_input.get("tasklist", "").lower()
            for tl in lists:
                if tasklist_name and tasklist_name not in tl["title"].lower():
                    continue
                tasks = service.tasks().list(tasklist=tl["id"]).execute().get("items", [])
                for t in tasks:
                    if query in t["title"].lower():
                        service.tasks().delete(tasklist=tl["id"], task=t["id"]).execute()
                        return f"Удалено: «{t['title']}»"
            return f"Задача «{tool_input['title']}» не найдена."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "tasks_delete_all":
        try:
            service = get_tasks_service()
            lists = service.tasklists().list().execute().get("items", [])
            tasklist_name = tool_input.get("tasklist", "").lower()
            deleted = []
            for tl in lists:
                if tasklist_name and tasklist_name not in tl["title"].lower():
                    continue
                tasks = service.tasks().list(tasklist=tl["id"]).execute().get("items", [])
                for t in tasks:
                    service.tasks().delete(tasklist=tl["id"], task=t["id"]).execute()
                    deleted.append(f"«{t['title']}»")
            if not deleted:
                return "Задач не найдено."
            return f"Удалено {len(deleted)} задач: {', '.join(deleted)}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "web_search":
        try:
            query = tool_input["query"]
            headers = {"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": os.getenv("BRAVE_API_KEY")}
            resp = requests.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params={"q": query, "count": 5})
            data = resp.json()
            results = data.get("web", {}).get("results", [])
            if not results:
                return "Ничего не найдено."
            output = []
            for r in results[:5]:
                output.append(f"**{r['title']}**\n{r.get('description', '')}\n{r['url']}")
            return "\n\n".join(output)
        except Exception as e:
            return f"Ошибка поиска: {e}"

    if name == "calendar_list_events":
        try:
            service = get_calendar_service()
            days = tool_input.get("days", 7)
            now = datetime.now(timezone.utc)
            from datetime import timedelta
            time_max = now + timedelta(days=days)

            events_result = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy="startTime"
            ).execute()

            events = events_result.get("items", [])
            if not events:
                return f"Событий на ближайшие {days} дней нет."

            result = []
            for e in events:
                start = e["start"].get("dateTime", e["start"].get("date", ""))
                end = e["end"].get("dateTime", e["end"].get("date", ""))
                if "T" in start:
                    dt_start = datetime.fromisoformat(start)
                    start_str = dt_start.strftime("%d.%m %H:%M")
                    if "T" in end:
                        dt_end = datetime.fromisoformat(end)
                        start_str += f"–{dt_end.strftime('%H:%M')}"
                else:
                    start_str = start
                result.append(f"• {start_str} — {e['summary']}")

            return "\n".join(result)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "calendar_create_event":
        try:
            service = get_calendar_service()
            title = tool_input["title"]
            date = tool_input["date"]
            time = tool_input["time"]
            duration = tool_input.get("duration_minutes", 60)
            description = tool_input.get("description", "")

            from datetime import timedelta
            start_dt = datetime.fromisoformat(f"{date}T{time}:00")
            end_dt = start_dt + timedelta(minutes=duration)

            event = {
                "summary": title,
                "description": description,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Almaty"},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Almaty"},
                "reminders": {"useDefault": True}
            }

            service.events().insert(calendarId="primary", body=event).execute()
            hours, mins = divmod(duration, 60)
            dur_str = f"{hours}ч" if mins == 0 else f"{hours}ч {mins}мин" if hours else f"{mins}мин"
            return f"Готово! «{title}» — {date} в {time}, длительность {dur_str}."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "calendar_delete_event":
        try:
            service = get_calendar_service()
            title = tool_input["title"]
            date = tool_input.get("date")

            now = datetime.now(timezone.utc)
            from datetime import timedelta
            time_max = now + timedelta(days=30)

            events_result = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                maxResults=50,
                singleEvents=True,
                orderBy="startTime",
                q=title
            ).execute()

            events = events_result.get("items", [])
            if not events:
                return f"Событие «{title}» не найдено."

            event = events[0]
            service.events().delete(calendarId="primary", eventId=event["id"]).execute()
            return f"Удалено: «{event['summary']}»"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "telegram_analyze_post":
        try:
            url = tool_input["url"]
            raw = _run_async_in_thread(_fetch_tg_post(url))
            return raw
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_send_draft":
        try:
            service = get_gmail_service()
            keyword = tool_input.get("keyword", "")
            # Ищем черновик по ключевому слову в теме
            drafts = service.users().drafts().list(userId="me").execute().get("drafts", [])
            target = None
            for d in drafts:
                draft = service.users().drafts().get(userId="me", id=d["id"], format="metadata").execute()
                headers = {h["name"]: h["value"] for h in draft["message"]["payload"]["headers"]}
                subject = headers.get("Subject", "")
                if keyword.lower() in subject.lower():
                    target = d["id"]
                    break
            if not target:
                # Если один черновик — берём его
                if len(drafts) == 1:
                    target = drafts[0]["id"]
                else:
                    return f"Черновик с '{keyword}' не найден. Доступно черновиков: {len(drafts)}."
            service.users().drafts().send(userId="me", body={"id": target}).execute()
            return f"Черновик отправлен."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "read_webpage":
        try:
            api_key = os.getenv("FIRECRAWL_API_KEY")
            if not api_key:
                logger.error("read_webpage: FIRECRAWL_API_KEY не задан в env")
                return "FIRECRAWL_API_KEY не задан."
            url = tool_input["url"]
            logger.info(f"read_webpage: запрос к Firecrawl для {url}, ключ ...{api_key[-4:]}")
            resp = requests.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["markdown"]},
                timeout=30
            )
            logger.info(f"read_webpage: статус {resp.status_code}, тело: {resp.text[:300]}")
            data = resp.json()
            if not data.get("success"):
                return f"Не удалось прочитать страницу: {data.get('error', resp.status_code)}"
            content = data.get("data", {}).get("markdown", "")
            if not content:
                return "Страница загрузилась, но контент пустой."
            return content[:8000]
        except Exception as e:
            logger.error(f"read_webpage: исключение — {e}")
            return f"Ошибка: {e}"

    if name == "search_flights":
        try:
            from flights import FlightsModule
            module = FlightsModule()
            return await module.search(
                origin=tool_input["origin"],
                destination=tool_input["destination"],
                month=tool_input["month"],
                max_price=tool_input.get("max_price"),
                direct_only=tool_input.get("direct_only", False),
                airline=tool_input.get("airline"),
                departure_time=tool_input.get("departure_time"),
                max_duration_hours=tool_input.get("max_duration_hours"),
                day_from=tool_input.get("day_from"),
                day_to=tool_input.get("day_to"),
                round_trip=tool_input.get("round_trip", False),
                return_month=tool_input.get("return_month"),
            )
        except Exception as e:
            return f"Ошибка поиска рейсов: {e}"

    return f"[Инструмент '{name}' не найден]"

# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(user_id: int, user_text: str, image_data: dict = None, send_photo=None) -> str:
    history = get_history(user_id)
    if image_data:
        default_photo_text = (
            "[Пользователь прислал фото без подписи. НЕ описывай содержимое. "
            "Посмотри предыдущие сообщения: если недавно отправляли фото на email — "
            "сразу отправь и это на тот же адрес через gmail_send. Иначе ответь ровно: "
            "\"Фото сохранено. Что сделать — отправить на email или в Drive?\"]"
        )
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": image_data["media_type"], "data": image_data["data"]}},
            {"type": "text", "text": user_text or default_photo_text}
        ]
    else:
        user_content = user_text
    history.append({"role": "user", "content": user_content})

    if len(history) > 60:
        history = history[-60:]
        # Убираем осиротевшие tool_result в начале истории:
        # если первое сообщение — user с tool_result блоками без предшествующего tool_use
        while history and history[0]["role"] == "user":
            content = history[0]["content"]
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                history = history[1:]  # убираем осиротевший tool_result
            else:
                break

    messages = list(history)
    user_tz = get_user_tz(user_id)
    now = datetime.now(user_tz)
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    system = SYSTEM_PROMPT.format(
        datetime=f"{now.strftime('%d.%m.%Y')}, {days[now.weekday()]}, {now.strftime('%H:%M')} ({user_tz.zone})"
    )
    # Добавляем долгосрочную память в системный промпт
    memories = get_user_memory(user_id)
    if memories:
        memory_lines = "\n".join(f"• {m['key']}: {m['value']}" for m in memories)
        system += f"\n\nДолгосрочная память о пользователе (факты из прошлых сессий):\n{memory_lines}"

    for _ in range(10):
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages
        )

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            text = "".join(
                block.text for block in assistant_content
                if hasattr(block, "text")
            )
            set_history(user_id, serialize_messages(messages))
            return text or "Готово."

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    logger.info(f"tool_use: {block.name}({block.input})")
                    result = await execute_tool(block.name, block.input, user_id)
                    logger.info(f"tool_result: {block.name} → {result[:200]}")
                    if result.startswith("IMAGE_URL:") and send_photo:
                        url = result[len("IMAGE_URL:"):]
                        await send_photo(url)
                        result = "Изображение сгенерировано и отправлено."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        break

    set_history(user_id, serialize_messages(messages))
    return "Не удалось получить ответ."

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send_reply(reply: str, message):
    """Отправляет ответ агента. Если FLIGHTS_BTN: — добавляет кнопку Aviasales."""
    if reply.startswith("FLIGHTS_BTN:"):
        rest = reply[len("FLIGHTS_BTN:"):]
        first_nl = rest.find("\n")
        btn_url = rest[:first_nl].strip()
        text_body = rest[first_nl + 1:].strip()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Перейти на Aviasales", url=btn_url)]])
        for i in range(0, len(text_body), 4096):
            await message.reply_text(
                text_body[i:i + 4096],
                parse_mode="HTML",
                reply_markup=keyboard if i == 0 else None
            )
    else:
        for i in range(0, len(reply), 4096):
            await message.reply_text(reply[i:i + 4096])

# ── Handlers ──────────────────────────────────────────────────────────────────

async def send_voice_reminder(bot, user_id: int, text: str):
    """Отправляет текстовое напоминание."""
    import re
    clean_text = re.sub(r'[^\w\s\.,!?:;\-\(\)«»"\']+', '', text).strip()
    await bot.send_message(chat_id=user_id, text=f"Напоминаю: {clean_text}")

def _brave_search(query: str, count: int = 8) -> list[dict]:
    """Выполняет поиск через Brave Search, возвращает список {title, description, url}."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": os.getenv("BRAVE_API_KEY"),
    }
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers=headers,
        params={"q": query, "count": count, "freshness": "pw"},  # pw = past week
        timeout=10,
    )
    return resp.json().get("web", {}).get("results", [])


def _hn_search(query: str) -> list[dict]:
    """Ищет в HackerNews через Algolia API, возвращает список {title, url, points}."""
    resp = requests.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": query, "tags": "story", "numericFilters": "created_at_i>{}".format(
            int(__import__("time").time()) - 7 * 24 * 3600
        ), "hitsPerPage": 10},
        timeout=10,
    )
    hits = resp.json().get("hits", [])
    return [{"title": h["title"], "url": h.get("url", ""), "points": h.get("points", 0)} for h in hits]


async def send_weekly_ai_digest(context):
    """Еженедельный дайджест по личным ИИ-ассистентам в Telegram на рынке СНГ — каждый пн в 12:00."""
    user_id = 661638470
    try:
        await context.bot.send_chat_action(chat_id=user_id, action="typing")

        # ── Brave Search: строго по нише ─────────────────────────────────────
        BRAVE_QUERIES = [
            "личный ИИ-ассистент Telegram запуск 2026",
            "Telegram бот ассистент Россия СНГ Казахстан",
            "Mira Telegram Cocoon обновление",
            "AI ассистент Telegram русский запуск",
        ]
        brave_items = []
        for q in BRAVE_QUERIES:
            try:
                results = _brave_search(q, count=6)
                for r in results:
                    brave_items.append(f"- {r['title']}: {r.get('description', '')[:120]} ({r['url']})")
            except Exception:
                pass

        # ── HackerNews: только по нашим ключевым словам ──────────────────────
        HN_QUERIES = ["AI agent", "AI assistant", "chatbot platform", "AI bot"]
        # Слова-исключения чтобы не тянуть общий LLM/ML шум
        EXCLUDE_WORDS = {"llm training", "fine-tuning", "dataset", "paper", "arxiv", "benchmark", "model weights"}
        hn_items = []
        seen_titles = set()
        for q in HN_QUERIES:
            try:
                hits = _hn_search(q)
                for h in hits:
                    title_low = h["title"].lower()
                    if h["title"] in seen_titles:
                        continue
                    if any(ex in title_low for ex in EXCLUDE_WORDS):
                        continue
                    seen_titles.add(h["title"])
                    hn_items.append(f"- [{h['points']}pts] {h['title']} ({h['url']})")
            except Exception:
                pass

        # ── Reddit (если настроен) ────────────────────────────────────────────
        reddit_items = []
        reddit_id = os.getenv("REDDIT_CLIENT_ID")
        reddit_secret = os.getenv("REDDIT_CLIENT_SECRET")
        if reddit_id and reddit_secret:
            try:
                import praw
                reddit = praw.Reddit(
                    client_id=reddit_id,
                    client_secret=reddit_secret,
                    user_agent=os.getenv("REDDIT_USER_AGENT", "tg-bot-digest/1.0"),
                )
                SUBREDDITS = ["AIAssistants", "chatbot", "artificial", "LocalLLaMA", "SaaS"]
                INCLUDE_WORDS = {"agent", "assistant", "chatbot", "bot", "copilot", "autonomous"}
                for sub_name in SUBREDDITS:
                    try:
                        sub = reddit.subreddit(sub_name)
                        for post in sub.top(time_filter="week", limit=15):
                            title_low = post.title.lower()
                            if not any(w in title_low for w in INCLUDE_WORDS):
                                continue
                            reddit_items.append(
                                f"- [r/{sub_name}, {post.score}↑] {post.title} ({post.url})"
                            )
                    except Exception:
                        pass
            except ImportError:
                pass

        # ── Формируем промпт для Claude ──────────────────────────────────────
        now = now_local()
        raw_data = []
        if brave_items:
            raw_data.append("=== WEB (Brave Search) ===\n" + "\n".join(brave_items[:30]))
        if hn_items:
            raw_data.append("=== HACKER NEWS ===\n" + "\n".join(hn_items[:20]))
        if reddit_items:
            raw_data.append("=== REDDIT ===\n" + "\n".join(reddit_items[:30]))

        if not raw_data:
            await context.bot.send_message(chat_id=user_id, text="⚠️ Недельный дайджест: не удалось получить данные.")
            return

        product_profile = """ПРОДУКТ — VELA (velabot.io):
Личный ИИ-ассистент в Telegram. Юзер регистрируется, вставляет токен от BotFather и получает свой брендированный бот под своим именем. Модули включаются автоматически по плану. Под капотом Claude API (Anthropic). Без кода, без серверов.

РЫНОК: B2C, русскоязычный СНГ (Казахстан, Россия, Беларусь, Украина). Активные люди, повседневные задачи. Цена $12-25/мес.

МОДУЛИ: погода, напоминания, курсы валют/криптовалют/акций, веб-поиск, утренний дайджест, фото-анализ и калории, авиабилеты, Google Calendar, Gmail, Drive, Tasks, генерация изображений, долгосрочная память, ценовые алерты.

ТАРИФЫ: Free ($0, 15 сообщений/день), Starter ($12/мес, 75 сообщений/день), Professional ($25/мес, безлимит).

ИЗВЕСТНЫЕ КОНКУРЕНТЫ (актуальный список на апрель 2026):
- Mira (mira.tg, @mira_ibot) — ГЛАВНАЯ УГРОЗА. Запущена февраль 2026. Официальный AI-ассистент Telegram на блокчейне Cocoon (TON Foundation, The Open Platform). 800+ интеграций через Mira Connect, Claude/GPT-5/Veo3, доступ к 1B+ юзеров TG. Free + Pro подписка через Telegram Stars.
- OK, Bob! (okbob.app, @okbob_bot) — узкоспециализированный task tracker + AI-чат /bob с памятью. Целевая аудитория — команды 3-10 человек.
- OpenClaw (openclaw.ai) — open-source self-hosted AI-агент для разработчиков. Вокруг есть SaaS-обёртки EaseClaw, KiloClaw, Zo.

ИЗ ПРЕЖНЕГО СПИСКА УБРАНЫ КАК НЕРЕЛЕВАНТНЫЕ: Salebot (воронки продаж B2B), PuzzleBot (конструктор сценариев B2B), Moltbot/BotFlow.

ТЕХНИЧЕСКАЯ БАЗА: Claude API с оплатой по токенам. НЕ перепродает подписки, НЕ использует чужие аккаунты. Инфраструктурные новости про блокировку подписок LLM-провайдерами к VELA НЕ относятся.

ОТЛИЧИЕ VELA: проще конкурентов (без сценариев и блок-схем), полностью на русском, фокус на личном использовании, свой брендированный бот через BotFather, оплата картой в долларах."""

        prompt = f"""Ты — конкурентный аналитик продукта VELA на рынке СНГ. Ниша VELA — личный ИИ-ассистент в Telegram. Рынок — Казахстан, Россия, Беларусь, Украина, остальной русскоязычный СНГ. Ниже профиль и сырые новости за неделю до {now.strftime('%d.%m.%Y')}.

{product_profile}

ЗАДАЧА:
Из новостей отбери ТОЛЬКО прямые угрозы продукту VELA в нише «личный ИИ-ассистент в Telegram на рынке СНГ». Будь максимально строгим — лучше пропустить, чем включить нерелевантное.

ВКЛЮЧАЙ ТОЛЬКО:
- Новый личный ИИ-ассистент в Telegram для русскоязычной/СНГ-аудитории
- Обновление прямых конкурентов (Mira, OK, Bob!, OpenClaw) если оно затрагивает рынок СНГ
- Telegram-бот личный ИИ-ассистент на базе Claude/GPT для обычных пользователей B2C (не бизнеса) с фокусом на СНГ или русский язык
- Выход Telegram, TON Foundation, Google, Apple в нишу личных ИИ-ассистентов с приходом на рынок СНГ

НЕ ВКЛЮЧАЙ (СТРОГО):
- Корпоративные enterprise-агенты и автоматизацию бизнеса
- Инфраструктурные новости (цены API, политика LLM-провайдеров, блокировки подписок) — это НЕ конкуренты
- Продукты на других платформах (SMS, WhatsApp, веб, мобильные приложения) — другой рынок
- ИИ-агенты общего назначения без привязки к Telegram
- Академические исследования, бенчмарки, датасеты, модели
- Общие новости про LLM и ИИ-индустрию

УРОВНИ УГРОЗЫ — строго по критериям:
🔴 — прямой конкурент: личный ИИ-ассистент в Telegram + B2C + без кода + рынок СНГ или русскоязычный + похожая цена + use-case пересекается с VELA на 50%+ (ассистент с памятью и интеграциями: почта/календарь/курсы/напоминания/задачи)
🟡 — косвенный: либо личный ИИ-ассистент в Telegram + B2C + без кода, но другой рынок/язык/аудитория (например, мировой EN-рынок); либо в Telegram + B2C, но другой use-case (write-helper в строке ввода, переводчик, суммаризатор чатов, генератор стикеров — без интеграций с почтой/календарём/финансами)
🟢 — слабая: другая платформа но та же концепция (личный ИИ-ассистент без кода для обычных людей)

КРИТИЧНО: не завышай уровень угрозы. Если продукт не в Telegram — это максимум 🟢. Если не B2C — это не конкурент вообще. Если продукт не на русскоязычной/СНГ-аудитории — это максимум 🟡.

КРИТЕРИЙ USE-CASE OVERLAP — обязательная проверка перед 🔴:
Сравни функционал продукта со списком модулей VELA (погода, напоминания, курсы валют/криптовалют/акций, веб-поиск, утренний дайджест, фото-анализ и калории, авиабилеты, Google Calendar/Gmail/Drive/Tasks, генерация изображений, долгосрочная память, ценовые алерты).
- Если продукт закрывает 50%+ этих модулей или близкие use-cases (почта, календарь, задачи, личная продуктивность с памятью) → 🔴
- Если продукт делает что-то другое (помощь в переписке, перевод сообщений, суммаризация чатов, групповые AI, креативные генераторы, развлекательный AI-собеседник) → максимум 🟡, даже если он в Telegram + B2C + дешёвый
Пример: Mira (AI-агент с памятью, поиском, генерацией) → 🔴. Telegram Premium AI (write-helper в строке ввода) → 🟡. ChatGPT-плагин для Telegram без интеграций → 🟡.

ФОРМАТ (строго):
- Никакого markdown: никаких **, *, #, _
- Каждый пункт: метка + название + тире + 1-2 предложения + (ссылка)
- Никаких --- разделителей

ЕСЛИ ЕСТЬ релевантные новости:
🎯 АНАЛИЗ КОНКУРЕНТОВ VELA ({now.strftime('%d.%m.%Y')})

🔴/🟡/🟢 Название — суть. (ссылка)

Вывод: 1-2 предложения.

ЕСЛИ НЕТ ничего релевантного (это нормально — большинство недель спокойные):
✅ Анализ конкурентов VELA ({now.strftime('%d.%m.%Y')}): прямых угроз за неделю не обнаружено. Рынок спокойный.

ДАННЫЕ:
{chr(10).join(raw_data)}"""

        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        digest_text = response.content[0].text

        for i in range(0, len(digest_text), 4096):
            await context.bot.send_message(chat_id=user_id, text=digest_text[i:i + 4096], disable_web_page_preview=True)

        # Сохраняем в историю — чтобы бот помнил что отправлял дайджест
        history = get_history(user_id)
        history.append({"role": "assistant", "content": f"[Автоматический конкурентный дайджест]\n{digest_text}"})
        set_history(user_id, history)

    except Exception as e:
        logger.error(f"Weekly digest error: {e}", exc_info=True)
        await context.bot.send_message(chat_id=user_id, text=f"⚠️ Ошибка недельного дайджеста: {e}")


_digest_sent_today = {}

async def check_morning_digest(context):
    user_id = 661638470
    if not is_morning_digest_enabled(user_id):
        return
    user_tz = get_user_tz(user_id)
    now = datetime.now(user_tz)
    today = now.strftime("%Y-%m-%d")
    if _digest_sent_today.get(user_id) == today:
        return
    h, m = get_digest_time(user_id)
    if now.hour == h and now.minute >= m:
        _digest_sent_today[user_id] = today
        await send_morning_digest(context)

async def send_morning_digest(context):
    user_id = 661638470
    try:
        now = now_local()
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
        date_str = f"{now.day} {months[now.month-1]}, {days[now.weekday()]}"
        lines = [f"Привет! Сегодня {date_str}", ""]

        # Погода — текущая + прогноз на день
        try:
            api_key = os.getenv("OPENWEATHER_API_KEY")
            resp = requests.get("https://api.openweathermap.org/data/2.5/weather",
                params={"q": "Almaty", "appid": api_key, "units": "metric", "lang": "ru"}, timeout=10)
            w = resp.json()
            desc = w["weather"][0]["description"].capitalize()
            temp = w["main"]["temp"]
            feels = w["main"]["feels_like"]
            wind = w["wind"]["speed"]
            icon = _weather_icon(desc)
            lines.append(f"{icon} Погода: {desc}, {temp:.0f}°C (ощущается {feels:.0f}°C), ветер {wind:.0f} м/с")

            # Прогноз на день — мин/макс и осадки
            resp2 = requests.get("https://api.openweathermap.org/data/2.5/forecast",
                params={"q": "Almaty", "appid": api_key, "units": "metric", "lang": "ru", "cnt": 8}, timeout=10)
            forecast = resp2.json()
            # Только дневные интервалы 7:00–21:00
            day_items = [i for i in forecast["list"]
                         if 7 <= datetime.fromisoformat(i["dt_txt"].replace(" ", "T")).hour <= 21]
            if not day_items:
                day_items = forecast["list"]
            temps = [i["main"]["temp"] for i in day_items]
            t_min, t_max = min(temps), max(temps)
            # Осадки только если вероятность >= 40% (pop = 0..1)
            rain = any(
                ("дождь" in i["weather"][0]["description"] or "rain" in i["weather"][0]["description"] or "ливень" in i["weather"][0]["description"])
                and i.get("pop", 0) >= 0.4
                for i in day_items
            )
            snow = any(
                ("снег" in i["weather"][0]["description"] or "snow" in i["weather"][0]["description"])
                and i.get("pop", 0) >= 0.4
                for i in day_items
            )
            precip = "Ожидается дождь, зонт пригодится." if rain else ("Ожидается снег." if snow else "Осадков не ожидается.")
            lines.append(f"Днём от {t_min:.0f} до {t_max:.0f}°C. {precip}")
            tip = _weather_tip(desc, temp, wind)
            if tip:
                lines.append(tip)
            lines.append("")
        except:
            pass

        # События на сегодня
        try:
            service = get_calendar_service()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=0)
            result = service.events().list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                maxResults=10, singleEvents=True, orderBy="startTime"
            ).execute()
            events = result.get("items", [])
            if events:
                lines.append("События:")
                for e in events:
                    start_t = e["start"].get("dateTime", e["start"].get("date", ""))
                    if "T" in start_t:
                        t = datetime.fromisoformat(start_t).strftime("%H:%M")
                        lines.append(f"• {t} — {e['summary']}")
                    else:
                        lines.append(f"• {e['summary']}")
                lines.append("")
        except:
            pass

        # Задачи (только список "Задачи", невыполненные)
        try:
            service = get_tasks_service()
            lists = service.tasklists().list().execute().get("items", [])
            task_lines = []
            for tl in lists:
                if "задач" in tl["title"].lower() or tl == lists[0]:
                    tasks = service.tasks().list(tasklist=tl["id"]).execute().get("items", [])
                    for t in tasks:
                        if t.get("status") != "completed":
                            task_lines.append(f"• {t['title']}")
                    break
            if task_lines:
                lines.append("Задачи:")
                lines.extend(task_lines[:10])
        except:
            pass

        morning_text = "\n".join(lines)
        await context.bot.send_message(chat_id=user_id, text=morning_text, disable_web_page_preview=True)

        # Сохраняем в историю — чтобы бот помнил что отправлял утренний дайджест
        history = get_history(user_id)
        history.append({"role": "assistant", "content": f"[Автоматический утренний дайджест]\n{morning_text}"})
        set_history(user_id, history)

    except Exception as e:
        logger.error(f"Дайджест ошибка: {e}")

async def check_price_alerts(context):
    if not redis_client:
        return
    for key in redis_client.scan_iter("price_alerts:*"):
        user_id = int(key.split(":")[1])
        alerts = get_price_alerts(user_id)
        if not alerts:
            continue
        remaining = []
        fired = False
        for a in alerts:
            price = fetch_asset_price(a["ticker"])
            if price is None:
                remaining.append(a)
                continue
            triggered = (a["direction"] == "above" and price >= a["target_price"]) or \
                        (a["direction"] == "below" and price <= a["target_price"])
            if triggered:
                direction_text = "достиг" if a["direction"] == "above" else "упал до"
                msg = f"🔔 {a['ticker']} {direction_text} ${price:,.2f} (цель: ${a['target_price']:,.2f})"
                try:
                    await context.bot.send_message(chat_id=user_id, text=msg)
                    fired = True
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления: {e}")
                    remaining.append(a)
            else:
                remaining.append(a)
        if len(remaining) != len(alerts):
            save_price_alerts(user_id, remaining)

async def check_reminders(context):
    if not redis_client:
        return
    now = now_local()
    for key in redis_client.scan_iter("reminders:*"):
        user_id = int(key.split(":")[1])
        reminders = get_reminders(user_id)
        changed = False
        for r in reminders:
            if not r.get("done") and TZ.localize(datetime.fromisoformat(r["at"]).replace(tzinfo=None)) <= now:
                r["done"] = True
                changed = True
                try:
                    await send_voice_reminder(context.bot, user_id, r["text"])
                except Exception as e:
                    logger.error(f"Ошибка отправки напоминания: {e}")
        if changed:
            save_reminders(user_id, reminders)

ALLOWED_USERS = {661638470}

def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ALLOWED_USERS:
            return
        return await func(update, context)
    return wrapper

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Вот что я умею:\n\n"
        "📅 Google Calendar\n"
        "— создать, посмотреть, удалить события\n\n"
        "📋 Google Tasks\n"
        "— создать, посмотреть, удалить задачи\n\n"
        "⏰ Напоминания\n"
        "— напомнить о чем-либо в нужное время\n\n"
        "🔔 Ценовые уведомления\n"
        "— напомнить, когда нужный актив достигнет определенной цены\n\n"
        "📧 Gmail\n"
        "— поиск, чтение, отправка, удаление, корзина, спам, отписка\n\n"
        "🗂 Google Drive\n"
        "— поиск, чтение, создание документов, таблиц, папок\n\n"
        "💱 Криптовалюты и валюты\n"
        "— курс любой криптовалюты или фиатной пары\n\n"
        "🌤 Погода\n"
        "— сейчас и прогноз до 5 дней\n\n"
        "📖 Чтение сайтов\n"
        "— открою ссылку и перескажу содержимое\n\n"
        "📸 Анализ фото\n"
        "— опишу и отвечу на вопросы по фото\n\n"
        "🍽 Анализ калорий по фото\n"
        "— подсчет ккал по фото еды или блюда\n\n"
        "✈️ Авиабилеты\n"
        "— поиск цен на рейсы в нужные даты по всему миру\n\n"
        "🧠 Долгосрочная память\n"
        "— помню факты из бесед между сессиями\n\n"
        "🌅 Утренний дайджест\n"
        "— погода, события и задачи на день\n\n"
        "📰 Конкурентный радар\n"
        "— обзор рынка ИИ-ботов раз в неделю\n\n"
        "📄 Документы\n"
        "— PDF, Word, текстовые файлы\n\n"
        "📊 Акции, индексы, драгметаллы и сырье\n"
        "🔍 Поиск в интернете\n"
        "🎨 Генерация изображений"
    )

@authorized
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("История очищена.")

@authorized
async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.message.reply_text(
            "Расскажи о себе — запомню навсегда.\n\n"
            "Пиши в свободной форме, например:\n"
            "Меня зовут Борис, 38 лет, живу в Алматы, строю SaaS для Telegram-ботов, слежу за BTC и ETH, занимаюсь биохакингом"
        )
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    injected = (
        f"Запомни о пользователе следующее — разбей на отдельные факты и сохрани каждый через memory_save. "
        f"После сохранения перечисли что именно запомнил.\n\n{args}"
    )
    try:
        reply = await run_agent(user_id, injected)
        await _send_reply(reply, update.message)
    except Exception as e:
        logger.error(f"cmd_about error: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при сохранении. Попробуй ещё раз.")

@authorized
async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    memories = get_user_memory(user_id)
    if not memories:
        await update.message.reply_text("Долгосрочная память пуста.")
        return
    lines = [f"✅ {m['key']}: {m['value']}" for m in memories]
    await update.message.reply_text("Что я о тебе знаю:\n" + "\n".join(lines))

@authorized
async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой user ID: {update.effective_user.id}")

@authorized
async def cmd_ai_agents_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Собираю дайджест, подожди 30-60 сек...")
    await send_weekly_ai_digest(context)

@authorized
async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = " ".join(context.args).strip().lower() if context.args else ""
    if not args:
        current = get_user_tz(user_id).zone
        await update.message.reply_text(
            f"Текущий часовой пояс: {current}\n\n"
            "Чтобы сменить:\n"
            "/timezone Europe/Moscow\n"
            "/timezone Asia/Almaty\n"
            "/timezone America/New_York\n\n"
            "Или название города: /timezone Москва"
        )
        return
    tz_name = CITY_TZ.get(args, args)
    try:
        pytz.timezone(tz_name)
        set_user_tz(user_id, tz_name)
        await update.message.reply_text(f"Часовой пояс установлен: {tz_name} ✓")
    except Exception:
        await update.message.reply_text(
            f"Неизвестный часовой пояс: {args}\n"
            "Используй стандартные названия: Europe/Moscow, Asia/Almaty, America/New_York и т.д."
        )


async def _upload_to_drive(file_bytes: bytes, filename: str, mime: str, update, context, folder_id: str = None):
    try:
        from googleapiclient.http import MediaInMemoryUpload
        service = get_drive_service()
        media = MediaInMemoryUpload(file_bytes, mimetype=mime)
        body = {"name": filename}
        if folder_id:
            body["parents"] = [folder_id]
        f = service.files().create(body=body, media_body=media, fields="id, name").execute()
        await update.message.reply_text(f"Файл '{f['name']}' загружен в Google Drive.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка загрузки в Drive: {e}")

def _get_or_create_drive_folder(service, folder_name: str) -> str:
    """Ищет папку по имени в Drive, создаёт если не найдена. Возвращает folder_id."""
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id"
    ).execute()
    return folder["id"]

# Буфер медиа-групп (альбомов): {group_id: {photos, caption, ...}}
_media_group_buffer: dict = {}
# Буфер для объединения нескольких альбомов от одного пользователя: {user_id: {count, task}}
_multi_album_buffer: dict = {}

# Буфер вложений для Gmail: {user_id: {bytes, filename, mime}}
_pending_attachments: dict = {}
# Timestamp последнего добавления фото в буфер: {user_id: float}
_pending_attachments_ts: dict = {}
# Буфер фото для контекста Claude: {user_id: {"media_type", "data"}}
_pending_photo: dict = {}

async def _send_multi_album_reply(user_id: int):
    """Ждёт 3с пока придут все альбомы, потом отвечает одним сообщением."""
    await asyncio.sleep(3)
    data = _multi_album_buffer.pop(user_id, None)
    if not data:
        return
    await data["update"].message.reply_text(f"📎 Сохранено {data['count']} фото. Что сделать — отправить на email или в Drive?")

async def _process_media_group(group_id: str, context):
    """Обрабатывает альбом фото после накопления всех сообщений."""
    await asyncio.sleep(1.5)  # Ждём пока придут все фото альбома
    if group_id not in _media_group_buffer:
        return
    data = _media_group_buffer.pop(group_id)
    update = data["first_update"]
    photos = data["photos"]
    upload_to_drive = data["upload_to_drive"]
    caption = data["caption"]
    user_id = data["user_id"]

    if upload_to_drive:
        # Определяем имя папки из caption (ищем "в папку <название>")
        folder_id = None
        import re
        m = re.search(r"в\s+папк[уе]\s+([^\s(,]+)", caption.lower())
        if m:
            folder_name = m.group(1).strip()
            try:
                service = get_drive_service()
                folder_id = _get_or_create_drive_folder(service, folder_name)
                await update.message.reply_text(f"Папка '{folder_name}' готова.")
            except Exception as e:
                await update.message.reply_text(f"Не удалось создать папку: {e}")

        for i, photo_bytes in enumerate(photos, start=1):
            fname = f"photo_{i}.jpg" if len(photos) > 1 else "photo.jpg"
            await _upload_to_drive(photo_bytes, fname, "image/jpeg", update, context, folder_id=folder_id)
        return

    # Не Drive-загрузка — сохраняем ВСЕ фото альбома в буфер для gmail_send
    import time as _time
    _pending_attachments[user_id] = [
        {"bytes": bytes(p), "filename": f"photo_{i+1}.jpg" if len(photos) > 1 else "photo.jpg", "mime": "image/jpeg"}
        for i, p in enumerate(photos)
    ]
    _pending_attachments_ts[user_id] = _time.time()

    # Альбом без подписи — буферизуем, ждём ещё альбомы от того же юзера
    if not caption:
        if user_id not in _multi_album_buffer:
            _multi_album_buffer[user_id] = {"count": len(photos), "update": update}
            asyncio.create_task(_send_multi_album_reply(user_id))
        else:
            _multi_album_buffer[user_id]["count"] += len(photos)
        return

    # Альбом с подписью — передаём первое фото в агент
    import base64
    image_data = {"media_type": "image/jpeg", "data": base64.b64encode(photos[0]).decode()}
    user_text = caption

    async def send_photo(url: str):
        await update.message.reply_photo(photo=url)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = await asyncio.wait_for(
            run_agent(user_id, user_text, image_data, send_photo=send_photo),
            timeout=120
        )
        await _send_reply(reply, update.message)
    except asyncio.TimeoutError:
        logger.error(f"run_agent TIMEOUT (120s) for user {user_id} (media_group)")
        await update.message.reply_text("Запрос занял слишком долго — попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Error for user {user_id}: {e}", exc_info=True)
        from anthropic import OverloadedError as _OverloadedError
        if isinstance(e, _OverloadedError):
            await update.message.reply_text("Серверы Claude сейчас перегружены — попробуй через минуту.")
        else:
            await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")

@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает голосовое сообщение, транскрибирует через Groq Whisper и передаёт в run_agent."""
    try:
        await context.bot.send_chat_action(update.effective_chat.id, action="typing")
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        voice_bytes = await tg_file.download_as_bytearray()

        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            await update.message.reply_text("GROQ_API_KEY не задан — голосовые сообщения недоступны.")
            return

        from groq import Groq as GroqClient
        import io as _io
        groq_client = GroqClient(api_key=groq_key)
        audio_file = _io.BytesIO(bytes(voice_bytes))
        audio_file.name = "voice.ogg"
        transcription = groq_client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=audio_file,
            language="ru",
        )
        transcript = transcription.text.strip()
        if not transcript:
            await update.message.reply_text("Не удалось распознать голосовое сообщение.")
            return

        # Пост-обработка транскрипта через Claude Haiku — исправляет ошибки Whisper
        try:
            import anthropic as _anthropic
            _cleanup_client = _anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            _cleanup_resp = _cleanup_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": (
                    "Исправь ошибки автоматической транскрипции русской речи: "
                    "расставь знаки препинания, исправь явные ошибки распознавания "
                    "(имена, технические термины, названия ИИ-моделей и продуктов). "
                    "Верни только исправленный текст, без пояснений.\n\n"
                    f"Текст:\n{transcript}"
                )}]
            )
            transcript = _cleanup_resp.content[0].text.strip()
        except Exception as _e:
            logger.warning(f"Transcript cleanup failed: {_e}")

        user_id = update.effective_user.id
        logger.info(f"Voice transcribed for user {user_id}: {transcript[:80]}")

        # Если голосовое переслано от другого человека — показываем транскрипт напрямую
        is_forwarded = (
            getattr(update.message, "forward_origin", None) is not None or
            getattr(update.message, "forward_from", None) is not None or
            getattr(update.message, "forward_sender_name", None) is not None
        )
        if is_forwarded:
            sender = ""
            origin = getattr(update.message, "forward_origin", None)
            if origin:
                # MessageOriginUser
                if hasattr(origin, "sender_user") and origin.sender_user:
                    sender = f" (от {origin.sender_user.full_name})"
                # MessageOriginHiddenUser
                elif hasattr(origin, "sender_user_name") and origin.sender_user_name:
                    sender = f" (от {origin.sender_user_name})"
                # MessageOriginChat
                elif hasattr(origin, "sender_chat") and origin.sender_chat:
                    sender = f" (из {origin.sender_chat.title})"
            # Fallback на старые поля
            if not sender:
                if getattr(update.message, "forward_from", None):
                    sender = f" (от {update.message.forward_from.full_name})"
                elif getattr(update.message, "forward_sender_name", None):
                    sender = f" (от {update.message.forward_sender_name})"
            await update.message.reply_text(f"Текст голосового{sender}:\n\n{transcript}")
            return

        # TTL для pending_attachments: чистим устаревшее
        import time as _time2
        if user_id in _pending_attachments_ts:
            if _time2.time() - _pending_attachments_ts[user_id] > 300:
                _pending_attachments.pop(user_id, None)
                _pending_attachments_ts.pop(user_id, None)

        # Подцепляем сохранённое фото к голосовому ТОЛЬКО если в транскрипте есть явное упоминание
        import base64 as _b64
        image_data = None
        photo_keywords = ["фото", "скриншот", "картин", "изображен", "снимк", "опиши", "что на", "что тут", "что здесь", "проанализируй"]
        if any(kw in transcript.lower() for kw in photo_keywords):
            pending = _pending_attachments.get(user_id)
            if isinstance(pending, list):
                pending = pending[0] if pending else None
            if pending and pending.get("mime", "").startswith("image/"):
                _pending_attachments.pop(user_id, None)
                image_data = {"media_type": pending["mime"], "data": _b64.b64encode(pending["bytes"]).decode()}

        async def send_photo(url: str):
            await update.message.reply_photo(photo=url)

        transcript_with_context = f"🎤 {transcript}"
        reply_to = update.message.reply_to_message
        if reply_to:
            reply_text = reply_to.text or reply_to.caption or ""
            if reply_text:
                if len(reply_text) > 500:
                    reply_text = reply_text[:500] + "..."
                transcript_with_context = f"[Отвечает на сообщение: «{reply_text}»]\n{transcript_with_context}"

        reply = await asyncio.wait_for(
            run_agent(user_id, transcript_with_context, image_data, send_photo=send_photo),
            timeout=120
        )
        await _send_reply(reply, update.message)

    except asyncio.TimeoutError:
        logger.error(f"run_agent TIMEOUT (120s) for user {user_id} (voice)")
        await update.message.reply_text("Запрос занял слишком долго — попробуй ещё раз.")
    except Exception as e:
        logger.error(f"handle_voice error: {e}", exc_info=True)
        await update.message.reply_text(f"Ошибка голосового: {type(e).__name__}: {e}")


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text or update.message.caption or ""

    # TTL для pending_attachments: если буфер старше 5 минут — чистим, чтобы старые фото не подтягивались
    import time as _time
    if user_id in _pending_attachments_ts:
        if _time.time() - _pending_attachments_ts[user_id] > 300:
            _pending_attachments.pop(user_id, None)
            _pending_attachments_ts.pop(user_id, None)

    # Если пользователь отвечает на конкретное сообщение — добавляем контекст
    reply_to = update.message.reply_to_message
    if reply_to:
        reply_text = reply_to.text or reply_to.caption or ""
        if reply_text:
            if len(reply_text) > 500:
                reply_text = reply_text[:500] + "..."
            user_text = f"[Отвечает на сообщение: «{reply_text}»]\n{user_text}"

    image_data = None

    # Загрузка файла в Drive если caption содержит "в drive" / "в драйв"
    caption_lower = (update.message.caption or "").lower()
    upload_to_drive = any(w in caption_lower for w in ["в drive", "в драйв", "сохрани в drive", "загрузи в drive"])

    if update.message.photo:
        import base64, io
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        # Альбом (несколько фото) — буферизуем все фото и обрабатываем вместе
        if update.message.media_group_id:
            group_id = update.message.media_group_id
            if group_id not in _media_group_buffer:
                _media_group_buffer[group_id] = {
                    "photos": [],
                    "caption": update.message.caption or "",
                    "upload_to_drive": upload_to_drive,
                    "first_update": update,
                    "user_id": user_id,
                }
                asyncio.create_task(_process_media_group(group_id, context))
            else:
                # Обновляем caption если у этого сообщения он есть
                if update.message.caption:
                    _media_group_buffer[group_id]["caption"] = update.message.caption
                    _media_group_buffer[group_id]["upload_to_drive"] = upload_to_drive
            _media_group_buffer[group_id]["photos"].append(bytes(file_bytes))
            return

        if upload_to_drive:
            await _upload_to_drive(bytes(file_bytes), "photo.jpg", "image/jpeg", update, context)
            return
        # Если есть текст — возможно img2img, загружаем на fal storage
        if user_text and any(w in user_text.lower() for w in ["измени", "перерисуй", "стиль", "сделай", "apply", "transform", "edit"]):
            try:
                import fal_client
                os.environ["FAL_KEY"] = os.getenv("FAL_API_KEY", "")
                uploaded = fal_client.upload(bytes(file_bytes), "image/jpeg")
                user_text = f"{user_text} [image_url:{uploaded}]"
            except Exception:
                pass
        # Добавляем фото в буфер вложений. Если прошло >5 минут с прошлого фото — сбрасываем буфер (новая тема)
        import time as _time
        now_ts = _time.time()
        last_ts = _pending_attachments_ts.get(user_id, 0)
        if now_ts - last_ts > 300:  # 5 минут
            _pending_attachments.pop(user_id, None)
        _pending_attachments_ts[user_id] = now_ts

        new_att = {"bytes": bytes(file_bytes), "filename": "photo.jpg", "mime": "image/jpeg"}
        existing = _pending_attachments.get(user_id)
        if existing is None:
            _pending_attachments[user_id] = new_att
        elif isinstance(existing, list):
            new_att["filename"] = f"photo_{len(existing)+1}.jpg"
            _pending_attachments[user_id].append(new_att)
        else:
            new_att["filename"] = "photo_2.jpg"
            _pending_attachments[user_id] = [existing, new_att]
        # Фото без подписи — сохраняем для контекста следующего сообщения
        if not user_text:
            _pending_photo[user_id] = {"media_type": "image/jpeg", "data": base64.b64encode(file_bytes).decode()}
            await update.message.reply_text("📎 Фото получено. Что сделать?")
            return
        image_data = {"media_type": "image/jpeg", "data": base64.b64encode(file_bytes).decode()}
    elif update.message.document:
        tg_file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        mime = update.message.document.mime_type or "application/octet-stream"
        fname = update.message.document.file_name or "file"
        if upload_to_drive:
            await _upload_to_drive(bytes(file_bytes), fname, mime, update, context)
            return
        # Сохраняем файл в буфер — может понадобиться как вложение к письму
        _pending_attachments[user_id] = {"bytes": bytes(file_bytes), "filename": fname, "mime": mime}
        if not user_text and not mime.startswith("image/") and mime not in ("text/plain", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"):
            await update.message.reply_text(f"📎 Файл сохранён: {fname}")
            return
        if mime.startswith("image/"):
            import base64
            image_data = {"media_type": mime, "data": base64.b64encode(file_bytes).decode()}
        elif mime == "text/plain":
            text_content = file_bytes.decode("utf-8", errors="ignore")[:4000]
            user_text = f"Содержимое файла {fname}:\n{text_content}\n\n{user_text}".strip()
        elif mime == "application/pdf":
            try:
                import io
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(bytes(file_bytes)))
                text_content = "\n".join(page.extract_text() or "" for page in reader.pages)[:4000]
                user_text = f"Содержимое PDF {fname}:\n{text_content}\n\n{user_text}".strip()
            except Exception:
                user_text = f"[PDF: {fname}] {user_text}".strip()
        elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"):
            try:
                import io
                from docx import Document
                doc = Document(io.BytesIO(bytes(file_bytes)))
                text_content = "\n".join(p.text for p in doc.paragraphs)[:4000]
                user_text = f"Содержимое документа {fname}:\n{text_content}\n\n{user_text}".strip()
            except Exception:
                user_text = f"[Word: {fname}] {user_text}".strip()
        else:
            user_text = f"[Файл: {fname}] {user_text}".strip()

    # Подхватить фото из буфера если текущее сообщение без фото
    if not image_data and user_id in _pending_photo:
        image_data = _pending_photo.pop(user_id)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    async def send_photo(url: str):
        await update.message.reply_photo(photo=url)

    try:
        reply = await asyncio.wait_for(
            run_agent(user_id, user_text, image_data, send_photo=send_photo),
            timeout=120
        )
        await _send_reply(reply, update.message)
    except asyncio.TimeoutError:
        logger.error(f"run_agent TIMEOUT (120s) for user {user_id}, text: {user_text[:100]}")
        await update.message.reply_text("Запрос занял слишком долго — попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Error for user {user_id}: {e}", exc_info=True)
        from anthropic import OverloadedError as _OverloadedError
        if isinstance(e, _OverloadedError):
            await update.message.reply_text("Серверы Claude сейчас перегружены — попробуй через минуту.")
        else:
            await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")

    app = Application.builder().token(token).build()
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    app.job_queue.run_repeating(check_price_alerts, interval=300, first=30)
    import datetime as dt
    app.job_queue.run_repeating(check_morning_digest, interval=60, first=15)
    app.job_queue.run_daily(send_weekly_ai_digest, time=dt.time(hour=12, minute=0, tzinfo=TZ), days=(1,))  # 1=пн (0=вс в ptb)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("ai_agents_digest", cmd_ai_agents_digest))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_message))

    # Регистрируем команды в меню Telegram
    from telegram import BotCommand
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start", "Начать"),
            BotCommand("clear", "Очистить историю чата"),
            BotCommand("myid", "Мой Telegram ID"),
            BotCommand("ai_agents_digest", "Дайджест по личным ИИ-ассистентам в Telegram (СНГ)"),
            BotCommand("timezone", "Часовой пояс"),
            BotCommand("memory", "Что бот знает обо мне"),
            BotCommand("about", "Рассказать о себе"),
        ])
    app.post_init = post_init

    logger.info("Бот запущен!")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Ошибка run_polling: {e}", exc_info=True)

if __name__ == "__main__":
    main()
