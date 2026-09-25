import json
import re
from urllib.error import URLError
from urllib.request import urlopen

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import APIException, ValidationError

from .ai_provider import MAX_AUDIO_BYTES
from .ai_tasks import create_ai_task
from .models import TelegramIntegration
from .telegram import TelegramError, bot_api


MENU = json.dumps({"inline_keyboard": [[{"text": "Create task", "callback_data": "task:create"}]]})
PROMPT = (
    "Create a task with Tiko\n\n"
    "Send a text or voice message describing the task, who should do it, and the deadline.\n\n"
    "Example:\n"
    "Assign Muslima Zokirjonova a task to fix the website's login page. "
    "Resolve the sign-in error and check that users can log in on both desktop and mobile. "
    "Set the deadline to 23 May next year and the priority to high.\n\n"
    "Use an existing employee's full name or email. You can write or speak in English, Uzbek, or Russian. "
    "If anything is unclear, I will ask you to resend the complete request with the missing details."
)


def send_menu(chat_id, text="Welcome to Tiko! Choose Create task to get started."):
    # Remove the old persistent keyboard before sending the new inline menu.
    bot_api("sendMessage", chat_id=chat_id, text=text,
            reply_markup=json.dumps({"remove_keyboard": True}))
    bot_api("sendMessage", chat_id=chat_id, text="What would you like to do?", reply_markup=MENU)


def handle_task_callback(callback):
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    sender = callback.get("from") or {}
    if not callback.get("id"):
        return
    connected = (chat.get("type") == "private" and sender.get("id") and
                 TelegramIntegration.objects.filter(
                     telegram_user_id=sender["id"], telegram_chat_id=chat.get("id"),
                     is_connected=True, user__is_active=True,
                 ).exists())
    if not connected:
        bot_api("answerCallbackQuery", callback_query_id=callback["id"],
                text="Connect Telegram from your TaskFlow profile first.", show_alert=True)
        return
    bot_api("answerCallbackQuery", callback_query_id=callback["id"])
    if callback.get("data") == "task:create":
        bot_api("sendMessage", chat_id=chat["id"], text=PROMPT, reply_markup=MENU)


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
    if text in {"/help", "/create", "Create task", "/cancel"}:
        bot_api("sendMessage", chat_id=chat_id, text=PROMPT, reply_markup=MENU)
        return
    voice = message.get("voice")
    if not (text or voice) or not message.get("message_id"):
        bot_api("sendMessage", chat_id=chat_id, text=PROMPT, reply_markup=MENU)
        return
    if text.startswith("/create "):
        text = text.split(maxsplit=1)[1]
    try:
        result = create_ai_task(
            user=integration.user, request_key=f"telegram:{chat_id}:{message['message_id']}",
            text=text, audio_loader=(lambda: download_voice(voice)) if voice else None,
            source_identity=voice.get("file_unique_id", voice.get("file_id", "")) if voice else "",
        )
        reply = result["message"]
        markup = MENU
        if result["status"] == "created":
            markup = json.dumps({"inline_keyboard": [
                [{"text": "Open task", "url": f"{settings.FRONTEND_URL.rstrip('/')}/tasks/{result['task']['id']}"}],
                [{"text": "Create another task", "callback_data": "task:create"}],
            ]})
    except APIException as exc:
        # A failed analysis rolls back the receipt so a fresh message can retry.
        reply = str(exc.detail)[:1500]
        markup = MENU
    bot_api("sendMessage", chat_id=chat_id, text=reply, reply_markup=markup)
