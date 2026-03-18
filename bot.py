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

def get_reminders(user_id: int) -> list:
    if redis_client:
        data = redis_client.get(f"reminders:{user_id}")
        return json.loads(data) if data else []
    return []

def save_reminders(user_id: int, reminders: list):
    if redis_client:
        redis_client.set(f"reminders:{user_id}", json.dumps(reminders, ensure_ascii=False))

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
Текущая дата и время: {datetime}
При создании событий используй временную зону Asia/Almaty (UTC+5) если не указано другое.
Когда показываешь события — форматируй красиво, с датой и временем.

Правило: если в сообщении пользователя есть [image_url:...] — это URL загруженного фото. Используй его в edit_image как image_url. КРИТИЧНО для промпта: FLUX img2img требует ПОЛНОЕ описание сцены + стиль. Сначала опиши что на фото (людей, фон, одежду), потом добавь стиль. Пример: "young Asian woman holding baby in carrier, indoor, cinematic film still, dramatic moody lighting, golden hour, 8k" — НЕ просто "cinematic style". Промпт всегда на английском.
Правило: когда спрашивают калории — отвечай кратко: название блюда и ккал. Если несколько — список и итого. Если на фото еда — определи блюда и дай калории по каждому и итого.
Правило: для курсов валют и крипты ВСЕГДА используй get_crypto_prices, не web_search.
Правило: для погоды ВСЕГДА используй get_weather, не web_search.

Правило: Calendar vs Tasks:
- Google Calendar — только если есть конкретная дата И время (встречи, события, звонки)
- Google Tasks, список «Задачи» — дела без времени, туду, напоминания, покупки
- Google Tasks, список «Идеи» — идеи, мысли, заметки, записать что-то на память

Контакты пользователя:
- Жена: Дана, dana.aristanbayeva@gmail.com"""

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
        "description": "Генерирует изображение по текстовому описанию через FLUX. Используй когда просят нарисовать, сгенерировать, создать картинку или изображение.",
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
        "description": "Отменяет напоминание по номеру из списка.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Номер напоминания из reminder_list (начиная с 1)"}
            },
            "required": ["index"]
        }
    },
    {
        "name": "get_token_info",
        "description": "Получает информацию о токене по адресу контракта (Solana, ETH, BSC и др.) через Dexscreener. Используй когда дают адрес контракта.",
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Адрес контракта токена"}
            },
            "required": ["address"]
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
                    "description": "Список криптовалют (CoinGecko ID): bitcoin, solana, ethereum и др. Оставь пустым если нужны только фиатные валюты."
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
        "name": "get_weather",
        "description": "Получает текущую погоду и прогноз на несколько дней для любого города.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Название города (например: Алматы, Москва, London)"},
                "forecast_days": {"type": "integer", "description": "Прогноз на N дней (0 = только сейчас, максимум 5)"}
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
    }
]

# ── Tool execution ────────────────────────────────────────────────────────────

def execute_tool(name: str, tool_input: dict, user_id: int = None) -> str:
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
            import re
            text = tool_input["text"]
            dt_str = tool_input["datetime"]
            now = now_local()

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
                remind_at = TZ.localize(datetime.fromisoformat(dt_str))

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
            lines = []
            for i, r in active:
                dt = datetime.fromisoformat(r["at"]).strftime("%d.%m %H:%M")
                lines.append(f"{i+1}. {dt} — {r['text']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "reminder_cancel":
        try:
            reminders = get_reminders(user_id)
            idx = tool_input["index"] - 1
            active = [(i, r) for i, r in enumerate(reminders) if not r.get("done")]
            if idx < 0 or idx >= len(active):
                return "Напоминание не найдено."
            real_idx, r = active[idx]
            reminders[real_idx]["done"] = True
            save_reminders(user_id, reminders)
            return f"Напоминание отменено: {r['text']}"
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "youtube_summary":
        try:
            import re
            url = tool_input["url"]
            match = re.search(r"(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})", url)
            if not match:
                return "Не удалось извлечь ID видео из ссылки."
            video_id = match.group(1)

            from youtube_transcript_api import YouTubeTranscriptApi
            transcript = None
            for langs in [["ru"], ["en"], None]:
                try:
                    if langs:
                        transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
                    else:
                        transcript = YouTubeTranscriptApi.get_transcript(video_id)
                    break
                except:
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

            return f"Субтитры недоступны для этого видео. Название: {title or 'неизвестно'}. URL: {url}"
        except Exception as e:
            return f"Не удалось получить транскрипт: {e}"

    if name == "get_token_info":
        try:
            address = tool_input["address"]
            resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
            data = resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                return f"Токен с адресом {address} не найден на Dexscreener."
            p = pairs[0]
            name_ = p.get("baseToken", {}).get("name", "?")
            symbol = p.get("baseToken", {}).get("symbol", "?")
            price = p.get("priceUsd", "?")
            change_1h = p.get("priceChange", {}).get("h1", 0)
            change_24h = p.get("priceChange", {}).get("h24", 0)
            vol_24h = p.get("volume", {}).get("h24", 0)
            liq = p.get("liquidity", {}).get("usd", 0)
            chain = p.get("chainId", "?")
            dex = p.get("dexId", "?")
            return (
                f"🔍 {name_} ({symbol}) на {chain}/{dex}\n"
                f"💲 Цена: ${price}\n"
                f"📈 1h: {change_1h:+.1f}% | 24h: {change_24h:+.1f}%\n"
                f"💧 Ликвидность: ${liq:,.0f}\n"
                f"📊 Объём 24h: ${vol_24h:,.0f}"
            )
        except Exception as e:
            return f"Ошибка: {e}"

    if name == "get_crypto_prices":
        try:
            coins = tool_input.get("coins", [])
            currencies = tool_input.get("currencies", [])
            lines = []

            # Крипта → USD
            if coins:
                ids = ",".join(coins)
                resp = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
                    headers={"Accept": "application/json"}
                )
                data = resp.json()
                names = {"bitcoin": "BTC", "solana": "SOL", "ethereum": "ETH", "tether": "USDT"}
                for coin in coins:
                    if coin in data:
                        price = data[coin]["usd"]
                        change = data[coin].get("usd_24h_change", 0)
                        arrow = "📈" if change >= 0 else "📉"
                        symbol = names.get(coin, coin.upper())
                        lines.append(f"{arrow} {symbol}: ${price:,.2f} ({change:+.1f}%)")
                    else:
                        lines.append(f"❓ {coin}: не найдено")

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

    if name == "get_weather":
        try:
            city = tool_input["city"]
            api_key = os.getenv("OPENWEATHER_API_KEY")
            forecast_days = tool_input.get("forecast_days", 0)

            # Текущая погода
            resp = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": city, "appid": api_key, "units": "metric", "lang": "ru"}
            )
            data = resp.json()
            if resp.status_code != 200:
                return f"Город не найден: {city}"

            desc = data["weather"][0]["description"].capitalize()
            temp = data["main"]["temp"]
            feels = data["main"]["feels_like"]
            humidity = data["main"]["humidity"]
            wind = data["wind"]["speed"]
            result = f"🌤 {city}\n{desc}, {temp:.0f}°C (ощущается {feels:.0f}°C)\nВлажность: {humidity}%, Ветер: {wind} м/с"

            if forecast_days and forecast_days > 0:
                resp2 = requests.get(
                    "https://api.openweathermap.org/data/2.5/forecast",
                    params={"q": city, "appid": api_key, "units": "metric", "lang": "ru", "cnt": forecast_days * 8}
                )
                forecast = resp2.json()
                seen_dates = set()
                forecast_lines = []
                for item in forecast.get("list", []):
                    date = item["dt_txt"][:10]
                    if date not in seen_dates and date != now_local().strftime("%Y-%m-%d"):
                        seen_dates.add(date)
                        t = item["main"]["temp"]
                        d = item["weather"][0]["description"]
                        forecast_lines.append(f"• {date}: {t:.0f}°C, {d}")
                    if len(seen_dates) >= forecast_days:
                        break
                if forecast_lines:
                    result += "\n\nПрогноз:\n" + "\n".join(forecast_lines)

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

async def run_agent(user_id: int, user_text: str, image_data: dict = None, send_photo=None) -> str:
    history = get_history(user_id)
    if image_data:
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": image_data["media_type"], "data": image_data["data"]}},
            {"type": "text", "text": user_text or "Что на этом изображении?"}
        ]
    else:
        user_content = user_text
    history.append({"role": "user", "content": user_content})

    if len(history) > 15:
        history = history[-15:]

    messages = list(history)
    now = now_local()
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
                    result = execute_tool(block.name, block.input, user_id)
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

# ── Handlers ──────────────────────────────────────────────────────────────────

async def send_voice_reminder(bot, user_id: int, text: str):
    """Отправляет текстовое напоминание."""
    import re
    clean_text = re.sub(r'[^\w\s\.,!?:;\-\(\)«»"\']+', '', text).strip()
    await bot.send_message(chat_id=user_id, text=f"Напоминание: {clean_text}")

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
    """Еженедельный дайджест трендов по ИИ-агентам/ассистентам/ботам — каждый пн в 12:00."""
    user_id = 661638470
    try:
        await context.bot.send_chat_action(chat_id=user_id, action="typing")

        # ── Brave Search: строго по нише ─────────────────────────────────────
        BRAVE_QUERIES = [
            "AI agent platform news this week",
            "AI assistant SaaS product launch 2026",
            "chatbot builder tool update this week",
            "AI bot automation agent trending",
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

        product_profile = """МОЙ ПРОДУКТ:
Персональный ИИ-ассистент в Telegram для обычных людей. Пользователь регистрируется, вставляет токен BotFather и включает нужные модули галочками (погода, напоминания, Google Calendar, почта, поиск, курсы валют, генерация изображений). Бот работает 24/7, отвечает на живом русском языке через Claude AI — без кода, без блок-схем, без технических знаний.

РЫНОК: B2C, русскоязычные СНГ (Казахстан, Россия, Украина, Беларусь). Целевая аудитория — фрилансеры, предприниматели, активные Telegram-пользователи. Цена $12/мес.

ОТЛИЧИЕ ОТ КОНКУРЕНТОВ: проще Moltbot (не нужны сценарии), дешевле и проще Base44/enterprise-агентов, полностью на русском, фокус на личном использовании а не автоматизации бизнеса."""

        prompt = f"""Ты — конкурентный аналитик стартапа. Ниже профиль продукта и сырые новости за неделю до {now.strftime('%d.%m.%Y')}.

{product_profile}

ЗАДАЧА:
Из новостей ниже отбери ТОЛЬКО те, что представляют прямую или косвенную угрозу/конкуренцию моему продукту.

ВКЛЮЧАЙ только если это:
- новый продукт/платформа с похожей концепцией (персональный ИИ-бот/ассистент без кода)
- обновление существующих конкурентов (Moltbot, BotFlow, аналоги)
- Telegram-бот на базе Claude/GPT для обычных пользователей (не бизнеса)
- платформа создания персональных ИИ-ассистентов без кода
- выход крупных игроков (Google, Telegram, Apple) в нишу персональных ИИ-ботов

НЕ ВКЛЮЧАЙ:
- корпоративные enterprise-агенты и автоматизацию бизнеса
- академические исследования и статистику рынка
- продукты без связи с Telegram или заменой Telegram-бота
- общие новости про LLM, модели, обучение

Правила оформления — строго:
- Никакого markdown: никаких **, *, #, _
- Каждый пункт начинается с метки угрозы: 🔴 (высокая), 🟡 (средняя), 🟢 (низкая)
- После метки: название, тире, 1-2 простых предложения почему это важно для меня
- Ссылку в скобках в конце пункта
- Никаких --- разделителей

Как определять уровень угрозы:
🔴 — прямой конкурент в Telegram для обычных пользователей, без кода, похожая цена
🟡 — косвенный конкурент или крупный игрок входящий в смежную нишу
🟢 — слабая угроза, другая аудитория или рынок, но стоит следить

ЕСЛИ ЕСТЬ релевантные новости, выдай в формате:
🎯 КОНКУРЕНТНЫЙ РАДАР ({now.strftime('%d.%m.%Y')})

🔴/🟡/🟢 Название — суть в 1-2 предложениях. (ссылка)

Вывод: 1-2 предложения — насколько серьёзна неделя.

ЕСЛИ НЕТ ничего релевантного, напиши только:
✅ Конкурентный радар ({now.strftime('%d.%m.%Y')}): явных новых угроз и прямых конкурентов за неделю не появилось. Рынок спокойный.

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
            await context.bot.send_message(chat_id=user_id, text=digest_text[i:i + 4096])

    except Exception as e:
        logger.error(f"Weekly digest error: {e}", exc_info=True)
        await context.bot.send_message(chat_id=user_id, text=f"⚠️ Ошибка недельного дайджеста: {e}")


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
                params={"q": "Almaty", "appid": api_key, "units": "metric", "lang": "ru"})
            w = resp.json()
            desc = w["weather"][0]["description"].capitalize()
            temp = w["main"]["temp"]
            feels = w["main"]["feels_like"]
            wind = w["wind"]["speed"]
            lines.append(f"Погода: {desc}, {temp:.0f}C (ощущается {feels:.0f}C), ветер {wind:.0f} м/с")

            # Прогноз на день — мин/макс и осадки
            resp2 = requests.get("https://api.openweathermap.org/data/2.5/forecast",
                params={"q": "Almaty", "appid": api_key, "units": "metric", "lang": "ru", "cnt": 8})
            forecast = resp2.json()
            temps = [i["main"]["temp"] for i in forecast["list"]]
            descriptions = [i["weather"][0]["description"] for i in forecast["list"]]
            t_min, t_max = min(temps), max(temps)
            rain = any("дождь" in d or "rain" in d or "ливень" in d for d in descriptions)
            snow = any("снег" in d or "snow" in d for d in descriptions)
            precip = "Ожидается дождь, зонт пригодится." if rain else ("Ожидается снег." if snow else "Осадков не ожидается.")
            lines.append(f"Днём от {t_min:.0f} до {t_max:.0f}C. {precip}")
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

        await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
    except Exception as e:
        logger.error(f"Дайджест ошибка: {e}")

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

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой user ID: {update.effective_user.id}")

async def cmd_ai_agents_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Собираю дайджест, подожди 30-60 сек...")
    await send_weekly_ai_digest(context)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/clear — очистить историю\n"
        "/myid — мой Telegram ID\n\n"
        "Google Calendar:\n"
        "показать события, создать, удалить\n\n"
        "Gmail:\n"
        "найти, прочитать, отправить письмо\n"
        "удалить, очистить корзину и спам\n\n"
        "Google Tasks:\n"
        "списки Задачи и Идеи — добавить, показать, выполнить\n\n"
        "Google Drive:\n"
        "поиск, чтение файлов\n"
        "создать doc / sheet / slides / папку\n"
        "переместить файл\n"
        "отправить файл/фото с подписью 'в drive' — загрузит в Drive\n\n"
        "Напоминания:\n"
        "поставить через N минут/часов или на время, список, отмена\n\n"
        "Погода — любой город, прогноз до 5 дней\n"
        "Валюты и крипта — BTC/SOL/ETH, USD/KZT и любые пары\n"
        "Токены — инфо по адресу контракта (Dexscreener)\n"
        "Веб-поиск — актуальная инфа через Brave\n"
        "Калории — текстом или фото еды\n"
        "Фото — анализ любого изображения\n"
        "Генерация изображений — по текстовому описанию (FLUX)\n"
        "PDF / Word — прочитает и ответит на вопросы\n\n"
        "Утренний дайджест в 11:00 — погода + события + задачи"
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

    # Не Drive-загрузка — отправляем в агент только первое фото (как раньше)
    import base64
    image_data = {"media_type": "image/jpeg", "data": base64.b64encode(photos[0]).decode()}
    user_text = caption

    async def send_photo(url: str):
        await update.message.reply_photo(photo=url)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = await run_agent(user_id, user_text, image_data, send_photo=send_photo)
        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i:i + 4096])
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text or update.message.caption or ""
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
        image_data = {"media_type": "image/jpeg", "data": base64.b64encode(file_bytes).decode()}
    elif update.message.document:
        tg_file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        mime = update.message.document.mime_type or "application/octet-stream"
        fname = update.message.document.file_name or "file"
        if upload_to_drive:
            await _upload_to_drive(bytes(file_bytes), fname, mime, update, context)
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

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    async def send_photo(url: str):
        await update.message.reply_photo(photo=url)

    try:
        reply = await run_agent(user_id, user_text, image_data, send_photo=send_photo)
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
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    import datetime as dt
    app.job_queue.run_daily(send_morning_digest, time=dt.time(hour=11, minute=0, tzinfo=TZ))
    app.job_queue.run_daily(send_weekly_ai_digest, time=dt.time(hour=12, minute=0, tzinfo=TZ), days=(0,))  # 0=пн
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("ai_agents_digest", cmd_ai_agents_digest))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Ошибка run_polling: {e}", exc_info=True)

if __name__ == "__main__":
    main()
