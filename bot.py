import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
conversations: dict[int, list] = {}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — личный ИИ-агент. Умный, краткий, полезный.
Отвечаешь на русском языке. Используй доступные инструменты когда нужно.
Если задача требует нескольких шагов — выполни их последовательно."""

# ── Tool definitions (что Claude видит) ──────────────────────────────────────

TOOLS = [
    {
        "name": "get_current_datetime",
        "description": "Возвращает текущую дату и время. Используй когда пользователь спрашивает который час, какой день, дата и т.п.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
    # Сюда будут добавляться новые инструменты:
    # google_calendar_list_events, gmail_send, notion_search, ...
]

# ── Tool execution (что реально выполняется) ──────────────────────────────────

def execute_tool(name: str, tool_input: dict) -> str:
    logger.info(f"Tool: {name}({json.dumps(tool_input, ensure_ascii=False)})")

    if name == "get_current_datetime":
        now = datetime.now()
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return f"{now.strftime('%d.%m.%Y')}, {days[now.weekday()]}, {now.strftime('%H:%M')}"

    return f"[Инструмент '{name}' не найден]"

# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(user_id: int, user_text: str) -> str:
    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": user_text})

    # Оставляем последние 40 сообщений
    if len(conversations[user_id]) > 40:
        conversations[user_id] = conversations[user_id][-40:]

    messages = list(conversations[user_id])

    # Цикл агента: Claude → tools → Claude → ... → финальный ответ
    for iteration in range(10):
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            # Финальный ответ — собираем текст
            text = "".join(
                block.text for block in assistant_content
                if hasattr(block, "text")
            )
            conversations[user_id] = messages
            return text or "Готово."

        if response.stop_reason == "tool_use":
            # Выполняем все вызовы инструментов
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

        # Неожиданный stop_reason
        break

    conversations[user_id] = messages
    return "Не удалось получить ответ."

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations[update.effective_user.id] = []
    await update.message.reply_text(
        "Привет! Я твой личный ИИ-агент на базе Claude.\n\n"
        "Просто пиши — отвечу на вопросы и выполню задачи.\n\n"
        "/clear — очистить историю\n"
        "/help — что умею"
    )

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations[update.effective_user.id] = []
    await update.message.reply_text("История очищена.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tools_list = "\n".join(f"• {t['name']}" for t in TOOLS)
    await update.message.reply_text(
        f"Активные инструменты:\n{tools_list}\n\n"
        "Скоро: Google Calendar, Gmail, Google Drive, Notion"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        reply = await run_agent(user_id, user_text)
        # Разбиваем длинные сообщения (лимит Telegram 4096 символов)
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
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Ошибка run_polling: {e}", exc_info=True)

if __name__ == "__main__":
    main()
