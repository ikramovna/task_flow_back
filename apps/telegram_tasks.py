import json
import logging
import re
from html import escape

from urllib.error import URLError
from urllib.request import urlopen

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import APIException, ValidationError

from .ai_provider import MAX_AUDIO_BYTES
from .ai_tasks import confirm_ai_delete, create_ai_task
from .digests import today_html
from .models import TelegramIntegration
from .telegram import TelegramError, bot_api, task_url, tasks_url

logger = logging.getLogger(__name__)

MENU = json.dumps({"inline_keyboard": [
    [{"text": "✍️ Text task", "callback_data": "task:create"},
     {"text": "🎙 Voice task", "callback_data": "task:voice"}],
    [{"text": "📅 Today", "callback_data": "task:today"},
     {"text": "💡 Example", "callback_data": "task:example"}],
    [{"text": "📋 Template", "callback_data": "task:template"},
     {"text": "❔ Help", "callback_data": "task:help"}],
]}, ensure_ascii=False)
BACK_MENU = json.dumps({"inline_keyboard": [
    [{"text": "💡 Example", "callback_data": "task:example"},
     {"text": "‹ Menu", "callback_data": "task:menu"}],
]}, ensure_ascii=False)
BOT_COMMANDS = [
    {"command": "menu", "description": "Main menu"},
    {"command": "create", "description": "New task (text)"},
    {"command": "voice", "description": "New task (voice)"},
    {"command": "today", "description": "My tasks for today"},
    {"command": "template", "description": "Task template"},
    {"command": "help", "description": "Help"},
]
HOME = (
    "👋 <b>Tiko</b> · task assistant\n\n"
    "Send a task as <b>text</b> or <b>voice</b>.\n"
    "<i>O‘zbek · Русский · English</i>"
)
PROMPT = (
    "✍️ <b>New task</b>\n\n"
    "Write in one message:\n"
    "• <b>What</b> to do\n"
    "• <b>Who</b> does it\n"
    "• <b>When</b> it’s due\n\n"
    "<i>Ali Valiyev, fix login page, Friday</i>"
)
TEMPLATE = (
    "📋 <b>Template</b>\n\n"
    "<pre>Task: \n"
    "Assign to: \n"
    "Deadline: \n"
    "Priority: Medium</pre>"
)
VOICE = (
    "🎙 <b>Voice task</b>\n\n"
    "Hold 🎤 and say <b>what</b>, <b>who</b>, <b>when</b>.\n"
    "<i>O‘zbek · Русский · English · up to 5 min</i>"
)
HELP = (
    "❔ <b>Help</b>\n\n"
    "<b>Create</b> — what + who + when\n"
    "<b>Edit</b> — “Edit last task: priority high”\n"
    "<b>Delete</b> — “Delete last task”\n"
    "<b>Today</b> — /today\n\n"
    "/menu — main menu"
)
EXAMPLE = (
    "💡 <b>Example</b>\n\n"
    "<pre>Muslima Zokirjonova, fix the login page, high priority, Friday</pre>\n"
    "<pre>Муслима, исправь страницу входа до пятницы</pre>\n"
    "<pre>Muslimaga login sahifani tuzatish, juma kunigacha</pre>"
)
SCREENS = {"menu": HOME, "create": PROMPT, "template": TEMPLATE,
           "voice": VOICE, "help": HELP, "example": EXAMPLE}


def screen_text(screen):
    return SCREENS[screen]


def screen_payload(chat_id, screen):
    return {"chat_id": chat_id, "text": screen_text(screen), "parse_mode": "HTML",
            "reply_markup": json.loads(MENU if screen == "menu" else BACK_MENU)}


def today_payload(chat_id, user):
    return {"chat_id": chat_id, "text": today_html(user), "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [
                [{"text": "Open tasks", "url": tasks_url()}],
                [{"text": "‹ Menu", "callback_data": "task:menu"}],
            ]}}


def send_payload(payload):
    payload = dict(payload)
    payload["reply_markup"] = json.dumps(payload["reply_markup"], ensure_ascii=False)
    bot_api("sendMessage", **payload)


def send_screen(chat_id, screen):
    send_payload(screen_payload(chat_id, screen))


def send_menu(chat_id, text="👋 Tiko is ready."):
    bot_api("sendMessage", chat_id=chat_id, text=text,
            reply_markup=json.dumps({"remove_keyboard": True}))
    send_screen(chat_id, "menu")


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
    if re.fullmatch(r"delete:[0-9A-Fa-f]{12}", screen):
        try:
            bot_api("answerCallbackQuery", callback_query_id=callback["id"], timeout=3)
        except TelegramError:
            logger.warning("Could not acknowledge Telegram delete callback")
        try:
            result = confirm_ai_delete(integration.user, screen.split(":", 1)[1])
            message_text = result["message"]
        except APIException as exc:
            message_text = str(exc.detail)
        return {"method": "sendMessage", "chat_id": chat["id"], "text": message_text}
    if screen not in SCREENS and screen != "today":
        return {"method": "answerCallbackQuery", "callback_query_id": callback["id"]}
    # Clear Telegram's loading indicator quickly. Telegram sends the selected
    # screen from the webhook response, avoiding a second outbound API request.
    try:
        bot_api("answerCallbackQuery", callback_query_id=callback["id"], timeout=3)
    except TelegramError:
        logger.warning("Could not acknowledge Telegram menu callback; sending screen via webhook response")
    if screen == "today":
        return {"method": "sendMessage", **today_payload(chat["id"], integration.user)}
    return {"method": "sendMessage", **screen_payload(chat["id"], screen)}


def download_voice(voice):
    if voice.get("file_size", 0) > MAX_AUDIO_BYTES or voice.get("duration", 0) > 300:
        raise ValidationError("Voice messages must not exceed 20 MB or 5 minutes.")
    file_id = voice.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise ValidationError("The voice file could not be found.")
    result = bot_api("getFile", file_id=file_id)
    path = result.get("file_path", "") if isinstance(result, dict) else ""
    if not re.fullmatch(r"[a-zA-Z0-9_/-]+\.[a-zA-Z0-9]+", path) or ".." in path:
        raise TelegramError("The Telegram voice file could not be downloaded.")
    try:
        with urlopen(f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{path}", timeout=20) as response:
            content = response.read(MAX_AUDIO_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise TelegramError("The Telegram voice file could not be downloaded.") from exc
    if len(content) > MAX_AUDIO_BYTES:
        raise ValidationError("Voice messages must not exceed 20 MB.")
    # Telegram voice messages use OGG/Opus, sometimes with an .oga filename.
    return SimpleUploadedFile("voice.ogg", content, content_type="audio/ogg")


def created_html(task):
    return (
        "✅ <b>Task created</b>\n\n"
        f"<b>{escape(task['title'])}</b>\n"
        f"👤 {escape(task['assignee_name'])}\n"
        f"📅 {escape(task['due_date'] or 'No deadline')} · ⚡ {escape(task['priority'].title())}"
    )


def handle_task_message(message):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "private" or not sender.get("id"):
        return
    integration = TelegramIntegration.objects.select_related("user").filter(
        telegram_user_id=sender["id"], telegram_chat_id=chat_id,
        is_connected=True, user__is_active=True,
    ).first()
    if not integration:
        bot_api("sendMessage", chat_id=chat_id, text="Open TaskFlow → Profile → Connect Telegram first.")
        return
    text = message.get("text", "").strip()
    if text in {"/start", "Task yaratish"}:
        send_menu(chat_id)
        return
    command = text.split("@", 1)[0] if " " not in text else text
    screens = {"/menu": "menu", "/create": "create", "Create task": "create",
               "/voice": "voice", "/template": "template", "/example": "example",
               "/help": "help", "/cancel": "menu"}
    if command == "/today":
        send_payload(today_payload(chat_id, integration.user))
        return
    if command in screens:
        send_screen(chat_id, screens[command])
        return
    voice = message.get("voice")
    if not (text or voice) or not message.get("message_id"):
        send_screen(chat_id, "menu")
        return
    if text.startswith("/create "):
        text = text.split(maxsplit=1)[1]
    try:
        result = create_ai_task(
            user=integration.user, request_key=f"telegram:{chat_id}:{message['message_id']}",
            text=text, audio_loader=(lambda: download_voice(voice)) if voice else None,
            source_identity=voice.get("file_unique_id", voice.get("file_id", "")) if voice else "",
        )
        reply = "❓ " + escape(result["message"])
        markup = MENU
        if result["status"] == "created":
            task = result["task"]
            reply = created_html(task)
            markup = json.dumps({"inline_keyboard": [
                [{"text": "Open task", "url": task_url(result["task"]["id"])},
                 {"text": "➕ New task", "callback_data": "task:create"}],
            ]})
        elif result["status"] == "updated":
            task = result["task"]
            reply = "✏️ <b>Task updated</b>\n\n" + escape(task["title"])
            markup = json.dumps({"inline_keyboard": [[
                {"text": "Open task", "url": task_url(task["id"])}
            ]]})
        elif result["status"] == "needs_confirmation":
            reply = "⚠️ <b>Delete?</b>\n\n" + escape(result["message"])
            markup = json.dumps({"inline_keyboard": [[
                {"text": "🗑 Delete", "callback_data": f"task:delete:{result['confirmation_code']}"}
            ]]})
        elif result["status"] == "deleted":
            reply = "🗑 <b>Task deleted</b>\n\n" + escape(result["task"]["title"])
            markup = MENU
        elif result.get("clarification_type") == "assignee":
            markup = None
    except APIException as exc:
        # A failed analysis rolls back the receipt so a fresh message can retry.
        reply = "⚠️ " + escape(str(exc.detail)[:1500])
        markup = MENU
    payload = {"chat_id": chat_id, "text": reply, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    bot_api("sendMessage", **payload)
