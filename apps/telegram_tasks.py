import json
import logging
import re
from html import escape

from django.utils import timezone
from urllib.error import URLError
from urllib.request import urlopen

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import APIException, ValidationError

from .ai_provider import MAX_AUDIO_BYTES
from .ai_tasks import create_ai_task
from .models import TelegramIntegration
from .telegram import TelegramError, bot_api, task_url

logger = logging.getLogger(__name__)

MENU = json.dumps({"inline_keyboard": [
    [{"text": "✍️ Create task", "callback_data": "task:create"},
     {"text": "🎙 Voice task", "callback_data": "task:voice"}],
    [{"text": "📋 Template", "callback_data": "task:template"},
     {"text": "💡 Example", "callback_data": "task:example"}],
    [{"text": "❔ Help", "callback_data": "task:help"}],
]}, ensure_ascii=False)
BACK_MENU = json.dumps({"inline_keyboard": [
    [{"text": "📋 Template", "callback_data": "task:template"},
     {"text": "💡 Example", "callback_data": "task:example"}],
    [{"text": "‹ Main menu", "callback_data": "task:menu"}],
]}, ensure_ascii=False)
BOT_COMMANDS = [
    {"command": "menu", "description": "Open the Tiko task menu"},
    {"command": "create", "description": "Create a task from text"},
    {"command": "voice", "description": "Create a task from a voice message"},
    {"command": "template", "description": "Copy a task template"},
    {"command": "example", "description": "See a complete task example"},
    {"command": "help", "description": "How Tiko works"},
]
HOME = (
    "<b>TIKO · TASKFLOW</b>\n"
    "<i>Your task assistant</i>\n\n"
    "Turn a message into an assigned task.\n"
    "Choose how you want to start below.\n\n"
    "✍️ <b>Text</b> — describe the work\n"
    "🎙 <b>Voice</b> — say it in your own words\n"
    "📋 <b>Template</b> — fill in the details\n\n"
    "<i>English · Uzbek · Russian</i>"
)
PROMPT = (
    "✍️ <b>CREATE A TASK</b>\n\n"
    "<b>1 · What needs to be done?</b>\nGive the task a clear title and describe the expected result.\n\n"
    "<b>2 · Who should do it?</b>\nUse an existing employee’s full name or email.\n\n"
    "<b>3 · When is it due?</b>\nInclude the day, month and year. Add a priority or project if needed.\n\n"
    "<i>Send your complete request as one message. Clear requests create a task immediately.</i>"
)
TEMPLATE = (
    "📋 <b>TASK TEMPLATE</b>\n\n"
    "Copy the block below, replace the brackets, and send it back.\n\n"
    "<pre>Create a task:\n"
    "Task title: [Short, clear title]\n"
    "Assign to: [Full name or email]\n"
    "Description: [Work to do and expected result]\n"
    "Priority: [Low / Medium / High]\n"
    "Deadline: [Day Month Year]\n"
    "Project: [Existing project name / No project]</pre>\n\n"
    "<i>Department comes from the assignee. Status starts as Not Started.</i>"
)
VOICE = (
    "🎙 <b>CREATE WITH YOUR VOICE</b>\n\n"
    "<b>Hold the microphone button</b> in Telegram and describe:\n\n"
    "• What needs to be done\n• The assignee’s full name\n• The deadline, including the year\n"
    "• Priority and project, if needed\n\n"
    "<i>You can speak Uzbek. The task title and description will be saved in English.</i>\n\n"
    "<i>Up to 5 minutes · 20 MB. Clear requests create a task immediately.</i>"
)
HELP = (
    "❔ <b>HOW TIKO WORKS</b>\n\n"
    "<b>Describe → Match → Create</b>\n"
    "AI extracts the task details. TaskFlow checks the assignee and your permissions, then saves the task.\n\n"
    "<b>If details are unclear</b>\nResend the complete corrected request, not just the missing name or date.\n\n"
    "<b>Defaults</b>\nDepartment: assignee’s department\nStatus: Not Started\n"
    "Priority: Medium if omitted\nEffort score: 1\nHidden: No\nCategory: empty\n\n"
    "<i>Use /menu whenever you want to return here.</i>"
)


def screen_text(screen):
    if screen == "example":
        today = timezone.localdate()
        year = today.year if (today.month, today.day) <= (5, 23) else today.year + 1
        return (
            "💡 <b>EXAMPLE TASK</b>\n\n"
            "A complete request ready to adapt:\n\n"
            "<pre>Create a task:\n"
            "Task title: Fix website login page sign-in error\n"
            "Assign to: Muslima Zokirjonova\n"
            "Description: Investigate and fix the sign-in error. Verify successful login on desktop and mobile.\n"
            "Priority: High\n"
            f"Deadline: 23 May {year}\n"
            "Project: No project</pre>\n\n"
            "<i>Replace the assignee with an existing employee and choose your actual deadline.</i>"
        )
    return {"menu": HOME, "create": PROMPT, "template": TEMPLATE,
            "voice": VOICE, "help": HELP}[screen]


def screen_payload(chat_id, screen):
    return {"chat_id": chat_id, "text": screen_text(screen), "parse_mode": "HTML",
            "reply_markup": json.loads(MENU if screen == "menu" else BACK_MENU)}


def send_screen(chat_id, screen):
    payload = screen_payload(chat_id, screen)
    payload["reply_markup"] = json.dumps(payload["reply_markup"], ensure_ascii=False)
    bot_api("sendMessage", **payload)


def send_menu(chat_id, text="Welcome to Tiko! Your task assistant is ready."):
    bot_api("sendMessage", chat_id=chat_id, text=text,
            reply_markup=json.dumps({"remove_keyboard": True}))
    send_screen(chat_id, "menu")


def handle_task_callback(callback):
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    sender = callback.get("from") or {}
    if not callback.get("id"):
        return None
    connected = (chat.get("type") == "private" and sender.get("id") and
                 TelegramIntegration.objects.filter(
                     telegram_user_id=sender["id"], telegram_chat_id=chat.get("id"),
                     is_connected=True, user__is_active=True,
                 ).exists())
    if not connected:
        return {"method": "answerCallbackQuery", "callback_query_id": callback["id"],
                "text": "Connect Telegram from your TaskFlow profile first.", "show_alert": True}
    screen = str(callback.get("data", "")).removeprefix("task:")
    if screen not in {"menu", "create", "voice", "template", "example", "help"}:
        return {"method": "answerCallbackQuery", "callback_query_id": callback["id"]}
    # Clear Telegram's loading indicator quickly. Telegram sends the selected
    # screen from the webhook response, avoiding a second outbound API request.
    try:
        bot_api("answerCallbackQuery", callback_query_id=callback["id"], timeout=3)
    except TelegramError:
        logger.warning("Could not acknowledge Telegram menu callback; sending screen via webhook response")
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
    if command == "/menu":
        send_screen(chat_id, "menu")
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
        reply = "<b>Let’s clarify a few details</b>\n\n" + escape(result["message"])
        markup = MENU
        if result["status"] == "created":
            task = result["task"]
            reply = (
                "✅ <b>TASK CREATED</b>\n\n"
                f"<b>{escape(task['title'])}</b>\n\n"
                f"👤 <b>Assigned to</b>  {escape(task['assignee_name'])}\n"
                f"📅 <b>Deadline</b>  {escape(task['due_date'] or 'Not specified')}\n"
                f"⚡ <b>Priority</b>  {escape(task['priority'].title())}\n"
                "📌 <b>Status</b>  Not Started"
            )
            markup = json.dumps({"inline_keyboard": [
                [{"text": "Open task", "url": task_url(result["task"]["id"])}],
                [{"text": "Create another task", "callback_data": "task:create"}],
            ]})
    except APIException as exc:
        # A failed analysis rolls back the receipt so a fresh message can retry.
        reply = "<b>Unable to create task</b>\n\n" + escape(str(exc.detail)[:1500])
        markup = MENU
    bot_api("sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML", reply_markup=markup)
