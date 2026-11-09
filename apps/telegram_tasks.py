import logging
import re
import threading
from html import escape

from urllib.error import URLError
from urllib.request import urlopen

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import APIException, ValidationError

from .ai_provider import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES
from .ai_schedule import build_schedule, choose_person, confirm_schedule, next_question, task_count
from .ai_tasks import cancel_pending, confirm_ai_assign_all, confirm_ai_delete, create_ai_task
from .digests import today_html
from .models import TelegramIntegration
from .telegram import TelegramError, bot_api, task_url, tasks_url

logger = logging.getLogger(__name__)

# Typing, a voice message or a photo creates tasks directly, so the menu only needs helpers.
MENU = {"inline_keyboard": [
    [{"text": "📅 Today", "callback_data": "task:today"},
     {"text": "💡 Example", "callback_data": "task:example"}],
    [{"text": "❔ How to use", "callback_data": "task:help"}],
]}
BOT_COMMANDS = [
    {"command": "today", "description": "My tasks for today"},
    {"command": "example", "description": "Examples"},
    {"command": "help", "description": "How to create a task"},
]
HOME = (
    "👋 <b>Tiko</b> creates tasks for you\n\n"
    "✍️ <b>Text</b> — just write it here\n"
    "🎤 <b>Voice</b> — hold the mic, speak\n"
    "📷 <b>Photo</b> — send a schedule\n\n"
    "Always say: <b>who</b> · <b>what</b> · <b>when</b>"
)
EXAMPLE = (
    "💡 <b>Examples</b>\n"
    "<i>who — what — when</i>\n\n"
    "👤 <b>Ali</b> — fix the login page — <b>Friday</b>\n\n"
    "👤 <b>Aliga</b> — hisobot tayyorlash — <b>ertaga</b>\n\n"
    "👥 <b>Всем</b> — обновить статусы — <b>до пятницы</b>\n\n"
    "📷 <b>Photo</b> of a schedule → many tasks"
)
HELP = (
    "❔ <b>How to create a task</b>\n\n"
    "✍️ <b>1. Text</b>\n"
    "Write who, what, when and send.\n"
    "<i>Ali — fix login page — Friday</i>\n\n"
    "🎤 <b>2. Voice</b>\n"
    "<b>Hold</b> the 🎤 button (bottom right), say who, what, when, then <b>release</b>.\n"
    "<i>O‘zbek · Русский · English</i>\n\n"
    "📷 <b>3. Photo</b>\n"
    "Send a schedule photo → pick names → <b>✅ Create</b>.\n\n"
    "👥 <b>Everyone</b> — start with “All” or “Hammaga”\n"
    "✏️ <b>Edit</b> — “Edit last task: high priority”\n"
    "🗑 <b>Delete</b> — “Delete last task”"
)
SCREENS = {"menu": HOME, "example": EXAMPLE, "help": HELP}
# Buttons and commands from older bot versions still open something sensible.
SCREEN_ALIASES = {"create": "menu", "voice": "help", "template": "menu"}
COMMANDS = {"/start": "menu", "/menu": "menu", "/cancel": "menu", "/create": "menu", "/voice": "help",
            "/template": "menu", "/example": "example", "/help": "help", "Task yaratish": "menu"}


def reply(chat_id, text, keyboard=None):
    """A bot reply returned in the webhook response: no extra (slow) Telegram API round trip."""
    payload = {"method": "sendMessage", "chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = keyboard
    return payload


def screen_payload(chat_id, screen):
    screen = SCREEN_ALIASES.get(screen, screen)
    return reply(chat_id, SCREENS[screen], MENU if screen == "menu" else None)


def today_payload(chat_id, user):
    return reply(chat_id, today_html(user), {"inline_keyboard": [[{"text": "Open tasks", "url": tasks_url()}]]})


def show_typing(chat_id):
    """Let the user see Tiko working while the AI reads the request."""
    if not settings.TELEGRAM_NOTIFY_IN_BACKGROUND:
        return

    def run():
        try:
            bot_api("sendChatAction", chat_id=chat_id, action="typing", timeout=5)
        except TelegramError:
            pass
    threading.Thread(target=run, daemon=True).start()


def created_html(result):
    task = result["task"]
    if result.get("action") == "assign_all":
        who = f"👥 {result['count']} employees · {', '.join(result.get('departments', []))}"
    else:
        who = f"👤 {task['assignee_name']}"
    details = [who, f"📅 {task['due_date'] or 'No deadline'}"]
    if task["priority"] != "medium":
        details.append(f"⚡ {task['priority'].title()}")
    text = f"✅ <b>{escape(task['title'])}</b>\n" + escape(" · ".join(details))
    if result.get("heard"):
        text += f"\n🎤 <i>{escape(result['heard'][:300])}</i>"
    return text


def created_payload(chat_id, result):
    url = tasks_url() if result.get("action") == "assign_all" else task_url(result["task"]["id"])
    return reply(chat_id, created_html(result), {"inline_keyboard": [[{"text": "Open", "url": url}]]})


def handle_task_callback(callback):
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    sender = callback.get("from") or {}
    if not callback.get("id"):
        return None
    integration = None
    if chat.get("type") == "private" and sender.get("id"):
        integration = TelegramIntegration.objects.select_related("user").filter(
            telegram_user_id=sender["id"], telegram_chat_id=chat.get("id"),
            is_connected=True, user__is_active=True,
        ).first()
    if not integration:
        return {"method": "answerCallbackQuery", "callback_query_id": callback["id"],
                "text": "Connect Telegram in your TaskFlow profile first.", "show_alert": True}
    screen = str(callback.get("data", "")).removeprefix("task:")
    step = re.fullmatch(
        r"cancel(?::([0-9A-Fa-f]{12}))?|(delete|all|sched):([0-9A-Fa-f]{12})"
        r"|pick:([0-9A-Fa-f]{12}):(\d{1,2}):(\d{1,2}):(\d|s)", screen)
    known = step or screen == "today" or screen in SCREENS or screen in SCREEN_ALIASES
    if not known:
        return {"method": "answerCallbackQuery", "callback_query_id": callback["id"]}
    # Clear Telegram's loading indicator quickly; the screen itself goes in the webhook response.
    try:
        bot_api("answerCallbackQuery", callback_query_id=callback["id"], timeout=3)
    except TelegramError:
        logger.warning("Could not acknowledge Telegram callback; replying via webhook response")
    if screen == "today":
        return today_payload(chat["id"], integration.user)
    if not step:
        return screen_payload(chat["id"], screen)
    # A confirmation step rewrites the question message itself, so no stale buttons stay behind.
    return as_edit(confirmation_step(integration.user, chat["id"], step), message.get("message_id"))


def as_edit(payload, message_id):
    if not message_id:
        return payload
    payload = {**payload, "method": "editMessageText", "message_id": message_id}
    payload.setdefault("reply_markup", {"inline_keyboard": []})
    return payload


def confirmation_step(user, chat_id, step):
    cancel_code, action, code, pick_code, item, person, choice = step.groups()
    if step.group(0).startswith("cancel"):
        if cancel_code:
            cancel_pending(user, cancel_code)
        return reply(chat_id, "✖️ Cancelled")
    if pick_code:
        return schedule_payload(chat_id, choose_person(user, pick_code, int(item), int(person), choice))
    if action == "sched":
        return schedule_payload(chat_id, confirm_schedule(user, code))
    try:
        if action == "delete":
            result = confirm_ai_delete(user, code)
            if result["status"] == "deleted":
                return reply(chat_id, "🗑 Deleted: " + escape(result["task"]["title"]))
            return reply(chat_id, "⚠️ " + escape(result["message"]))
        result = confirm_ai_assign_all(user, code)
    except APIException as exc:
        return reply(chat_id, "⚠️ " + escape(str(exc.detail)[:500]))
    if result["status"] != "created":
        return reply(chat_id, "⚠️ " + escape(result["message"]))
    return created_payload(chat_id, result)


def download_file(file_id, max_bytes, label):
    if not isinstance(file_id, str) or not file_id:
        raise ValidationError(f"The {label} could not be found.")
    result = bot_api("getFile", file_id=file_id)
    path = result.get("file_path", "") if isinstance(result, dict) else ""
    if not re.fullmatch(r"[a-zA-Z0-9_/-]+\.[a-zA-Z0-9]+", path) or ".." in path:
        raise TelegramError(f"The Telegram {label} could not be downloaded.")
    try:
        with urlopen(f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{path}", timeout=20) as response:
            content = response.read(max_bytes + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"The Telegram {label} could not be downloaded.") from exc
    if len(content) > max_bytes:
        raise ValidationError(f"The {label} is too large.")
    return content


def download_voice(voice):
    if voice.get("file_size", 0) > MAX_AUDIO_BYTES or voice.get("duration", 0) > 300:
        raise ValidationError("Voice messages must not exceed 20 MB or 5 minutes.")
    content = download_file(voice.get("file_id"), MAX_AUDIO_BYTES, "voice file")
    # Telegram voice messages use OGG/Opus, sometimes with an .oga filename.
    return SimpleUploadedFile("voice.ogg", content, content_type="audio/ogg")


IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def message_image(message):
    """The largest photo size, or an image sent as a file; returns (file_id, unique_id, mime type)."""
    photos = message.get("photo") or []
    if photos:
        best = max(photos, key=lambda size: size.get("file_size") or size.get("width", 0) * size.get("height", 0))
        return best.get("file_id"), best.get("file_unique_id", ""), "image/jpeg", best.get("file_size", 0)
    document = message.get("document") or {}
    if document.get("mime_type") in IMAGE_TYPES:
        return (document.get("file_id"), document.get("file_unique_id", ""), document["mime_type"],
                document.get("file_size", 0))
    return None


def schedule_payload(chat_id, result):
    if result["status"] == "created":
        text = f"✅ <b>Created {result['count']} tasks</b>"
        if result.get("failed"):
            text += f"\n⚠️ {result['failed']} not created (outside your departments)."
        return reply(chat_id, text, {"inline_keyboard": [[{"text": "Open tasks", "url": tasks_url()}]]})
    if result["status"] != "needs_confirmation":
        return reply(chat_id, "❓ " + escape(result["message"]))
    code = result["confirmation_code"]
    question = next_question(result)
    if question:
        i, j = question
        item, person = result["items"][i], result["items"][i]["people"][j]
        buttons = [[{"text": candidate["name"][:60], "callback_data": f"task:pick:{code}:{i}:{j}:{k}"}]
                   for k, candidate in enumerate(person["candidates"])]
        buttons.append([{"text": "Skip", "callback_data": f"task:pick:{code}:{i}:{j}:s"}])
        text = (f"👤 Which <b>{escape(person['name'])}</b>?\n"
                f"<i>{escape(item['title'])} · {item['date'][8:]}.{item['date'][5:7]}</i>")
        return reply(chat_id, text, {"inline_keyboard": buttons})
    lines = []
    for item in result["items"]:
        names = [person["label"] for person in item["people"] if person["status"] == "ok"]
        missing = [person["name"] for person in item["people"] if person["status"] == "unknown"]
        when = f"{item['date'][8:]}.{item['date'][5:7]}" + (f" {item['time']}" if item["time"] else "")
        who = ", ".join(names) if names else "—"
        if missing:
            who += f" (❔ {', '.join(missing)})"
        lines.append(f"• {when} {item['title']} → {who}")
    count = task_count(result)
    text = f"🗓 <b>{count} tasks</b>\n\n" + escape("\n".join(lines)[:3000])
    notes = []
    if any(person["status"] == "unknown" for item in result["items"] for person in item["people"]):
        notes.append("❔ = name not found, skipped")
    if result.get("skipped"):
        notes.append(f"{result['skipped']} past or unnamed events skipped")
    if notes:
        text += "\n\n<i>" + escape(" · ".join(notes)) + "</i>"
    if not count:
        return reply(chat_id, text + "\n\nNo one to assign.")
    return reply(chat_id, text, {"inline_keyboard": [[
        {"text": f"✅ Create {count}", "callback_data": f"task:sched:{code}"},
        {"text": "✖️ Cancel", "callback_data": f"task:cancel:{code}"},
    ]]})


def result_payload(chat_id, result):
    status = result["status"]
    if status == "created":
        return created_payload(chat_id, result)
    if status == "updated":
        task = result["task"]
        return reply(chat_id, "✏️ Updated: <b>" + escape(task["title"]) + "</b>",
                     {"inline_keyboard": [[{"text": "Open", "url": task_url(task["id"])}]]})
    if status == "deleted":
        return reply(chat_id, "🗑 Deleted: " + escape(result["task"]["title"]))
    if status == "needs_confirmation" and result.get("action") == "assign_all":
        pending = result["pending_task"]
        text = (f"👥 Send <b>{escape(pending['title'])}</b> to {len(result['member_ids'])} employees "
                f"({escape(', '.join(result['departments']))})?")
        return reply(chat_id, text, {"inline_keyboard": [[
            {"text": "✅ Send", "callback_data": f"task:all:{result['confirmation_code']}"},
            {"text": "✖️ Cancel", "callback_data": f"task:cancel:{result['confirmation_code']}"},
        ]]})
    if status == "needs_confirmation":
        return reply(chat_id, f"🗑 Delete <b>{escape(result['task']['title'])}</b>?", {"inline_keyboard": [[
            {"text": "🗑 Delete", "callback_data": f"task:delete:{result['confirmation_code']}"},
            {"text": "✖️ Cancel", "callback_data": f"task:cancel:{result['confirmation_code']}"},
        ]]})
    return reply(chat_id, "❓ " + escape(result["message"]))


def handle_task_message(message):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "private" or not sender.get("id"):
        return None
    integration = TelegramIntegration.objects.select_related("user").filter(
        telegram_user_id=sender["id"], telegram_chat_id=chat_id,
        is_connected=True, user__is_active=True,
    ).first()
    if not integration:
        return reply(chat_id, "Connect Telegram in TaskFlow → Profile first.")
    text = message.get("text", "").strip()
    command = text.split("@", 1)[0] if " " not in text else text
    if command == "/today":
        return today_payload(chat_id, integration.user)
    if command in COMMANDS:
        return screen_payload(chat_id, COMMANDS[command])
    image = message_image(message)
    if image and message.get("message_id"):
        file_id, unique_id, mime_type, size = image
        if size and size > MAX_IMAGE_BYTES:
            return reply(chat_id, "⚠️ The image must be under 10 MB.")
        show_typing(chat_id)
        try:
            result = build_schedule(
                user=integration.user, request_key=f"telegram:{chat_id}:{message['message_id']}",
                image_loader=lambda: (download_file(file_id, MAX_IMAGE_BYTES, "image"), mime_type),
                source_identity=unique_id or file_id, note=message.get("caption", ""),
            )
        except APIException as exc:
            return reply(chat_id, "⚠️ " + escape(str(exc.detail)[:500]))
        return schedule_payload(chat_id, result)
    voice = message.get("voice")
    if not (text or voice) or not message.get("message_id"):
        return screen_payload(chat_id, "menu")
    if text.startswith("/create "):
        text = text.split(maxsplit=1)[1]
    show_typing(chat_id)
    try:
        result = create_ai_task(
            user=integration.user, request_key=f"telegram:{chat_id}:{message['message_id']}",
            text=text, audio_loader=(lambda: download_voice(voice)) if voice else None,
            source_identity=voice.get("file_unique_id", voice.get("file_id", "")) if voice else "",
        )
    except APIException as exc:
        # A failed analysis rolls back the receipt so a fresh message can retry.
        return reply(chat_id, "⚠️ " + escape(str(exc.detail)[:1500]))
    return result_payload(chat_id, result)
