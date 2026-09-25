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


MENU = json.dumps({"keyboard": [[{"text": "Task yaratish"}]], "resize_keyboard": True})
PROMPT = (
    "Task yaratish uchun matn yoki ovoz yuboring. Masalan: "
    "Muslima Zokirjonovaga websiteni fix qilish, deadline 23 may. "
    "Xodim topilmasa, to‘liq ism yoki email bilan vazifani qayta yuboring."
)


def download_voice(voice):
    if voice.get("file_size", 0) > MAX_AUDIO_BYTES or voice.get("duration", 0) > 300:
        raise ValidationError("Ovoz 20 MB va 5 daqiqadan oshmasligi kerak.")
    file_id = voice.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise ValidationError("Ovoz fayli topilmadi.")
    result = bot_api("getFile", file_id=file_id)
    path = result.get("file_path", "") if isinstance(result, dict) else ""
    if not re.fullmatch(r"[a-zA-Z0-9_/-]+\.[a-zA-Z0-9]+", path) or ".." in path:
        raise TelegramError("Telegram ovoz faylini yuklab bo‘lmadi.")
    try:
        with urlopen(f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{path}", timeout=20) as response:
            content = response.read(MAX_AUDIO_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise TelegramError("Telegram ovoz faylini yuklab bo‘lmadi.") from exc
    if len(content) > MAX_AUDIO_BYTES:
        raise ValidationError("Ovoz 20 MB dan oshmasligi kerak.")
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
    if text in {"/start", "/help", "/create", "Task yaratish", "/cancel"}:
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
        if result["status"] == "created":
            reply += f"\n{settings.FRONTEND_URL.rstrip('/')}/tasks/{result['task']['id']}"
    except APIException as exc:
        # A failed analysis rolls back the receipt so a fresh message can retry.
        reply = str(exc.detail)[:1500]
    bot_api("sendMessage", chat_id=chat_id, text=reply, reply_markup=MENU)
