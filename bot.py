import os
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

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
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    )

def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_creds())

def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_creds())

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — личный ИИ-агент. Умный, краткий, полезный.
Отвечаешь на русском языке. Используй доступные инструменты когда нужно.
Текущая дата и время: {datetime}
При создании событий используй временную зону Asia/Almaty (UTC+5) если не указано другое.
Когда показываешь события — форматируй красиво, с датой и временем."""

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
                "description": {"type": "string", "description": "Описание события (необязательно)"},
                "reminder_minutes": {"type": "integer", "description": "За сколько минут напомнить (по умолчанию 30)"}
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
        "description": "Отправляет письмо. Используй когда пользователь просит написать или отправить письмо.",
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
    }
]

# ── Tool execution ────────────────────────────────────────────────────────────

def execute_tool(name: str, tool_input: dict) -> str:
    logger.info(f"Tool: {name}({json.dumps(tool_input, ensure_ascii=False)})")

    if name == "get_current_datetime":
        now = datetime.now()
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
            import base64
            service = get_gmail_service()
            msg = service.users().messages().get(userId="me", id=tool_input["message_id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            body = ""
            if "parts" in msg["payload"]:
                for part in msg["payload"]["parts"]:
                    if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                        body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                        break
            elif "data" in msg["payload"].get("body", {}):
                body = base64.urlsafe_b64decode(msg["payload"]["body"]["data"]).decode("utf-8", errors="ignore")
            return f"От: {headers.get('From','?')}\nКому: {headers.get('To','?')}\nТема: {headers.get('Subject','?')}\nДата: {headers.get('Date','?')}\n\n{body[:3000]}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_send":
        try:
            import base64
            from email.mime.text import MIMEText
            service = get_gmail_service()
            msg = MIMEText(tool_input["body"])
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
            return f"Письмо отправлено на {tool_input['to']}."
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "gmail_trash":
        try:
            service = get_gmail_service()
            service.users().messages().trash(userId="me", id=tool_input["message_id"]).execute()
            return "Письмо перемещено в корзину."
        except Exception as e:
            return f"Ошибка: {e}"

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
                if "T" in start:
                    dt = datetime.fromisoformat(start)
                    start_str = dt.strftime("%d.%m %H:%M")
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
            reminder = tool_input.get("reminder_minutes", 30)
            description = tool_input.get("description", "")

            from datetime import timedelta
            start_dt = datetime.fromisoformat(f"{date}T{time}:00")
            end_dt = start_dt + timedelta(minutes=duration)

            event = {
                "summary": title,
                "description": description,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Almaty"},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Almaty"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": reminder},
                        {"method": "email", "minutes": reminder}
                    ]
                }
            }

            created = service.events().insert(calendarId="primary", body=event).execute()
            return f"Создано: «{title}» {date} в {time}. Напоминание за {reminder} мин."
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

    return f"[Инструмент '{name}' не найден]"

# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(user_id: int, user_text: str) -> str:
    history = get_history(user_id)
    history.append({"role": "user", "content": user_text})

    if len(history) > 40:
        history = history[-40:]

    messages = list(history)
    now = datetime.now()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    system = SYSTEM_PROMPT.format(
        datetime=f"{now.strftime('%d.%m.%Y')}, {days[now.weekday()]}, {now.strftime('%H:%M')}"
    )

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
                    result = execute_tool(block.name, block.input)
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

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я твой личный ИИ-агент на базе Claude.\n\n"
        "Что умею:\n"
        "• Отвечаю на вопросы\n"
        "• Google Calendar — показать, создать, удалить события\n\n"
        "Примеры:\n"
        "«Что у меня на этой неделе?»\n"
        "«Добавь встречу в пятницу в 15:00»\n"
        "«Удали встречу с Иваном»\n\n"
        "/clear — очистить историю"
    )

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("История очищена.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = await run_agent(user_id, user_text)
        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i:i + 4096])
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Ошибка run_polling: {e}", exc_info=True)

if __name__ == "__main__":
    main()
