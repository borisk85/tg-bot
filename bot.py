import os
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
conversations: dict[int, list] = {}

# ── Google Calendar client ────────────────────────────────────────────────────

def get_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)

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
    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": user_text})

    if len(conversations[user_id]) > 40:
        conversations[user_id] = conversations[user_id][-40:]

    messages = list(conversations[user_id])
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
            conversations[user_id] = messages
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

    conversations[user_id] = messages
    return "Не удалось получить ответ."

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations[update.effective_user.id] = []
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
    conversations[update.effective_user.id] = []
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
