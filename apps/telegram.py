import json
import logging
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.urls import reverse

from .models import TelegramIntegration

logger = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


def bot_api(method, **payload):
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured.")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TelegramError("Telegram service is unavailable.") from exc
    if not result.get("ok"):
        raise TelegramError(result.get("description", "Telegram rejected the request."))
    return result.get("result")


def task_url(task):
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/tasks/{task.pk}"


def notification_text(notification):
    task = notification.task
    lines = [f"<b>{escape(notification.title)}</b>", "", escape(notification.body)]
    if task:
        lines = [f"<b>{escape(notification.title)}</b>", "", f"Task: <b>{escape(task.title)}</b>"]
        if notification.actor:
            lines.append(f"Assigned by: {escape(notification.actor.get_full_name() or notification.actor.email)}")
        lines.append(f"Priority: {escape(task.get_priority_display())}")
        if task.due_date:
            lines.append(f"Deadline: {task.due_date:%d %b %Y}")
    return "\n".join(lines)


def send_notification(notification):
    try:
        integration = TelegramIntegration.objects.get(
            user=notification.recipient,
            is_connected=True,
            notifications_enabled=True,
        )
    except TelegramIntegration.DoesNotExist:
        return False
    payload = {
        "chat_id": integration.telegram_chat_id,
        "text": notification_text(notification),
        "parse_mode": "HTML",
    }
    if notification.task:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": "Open Task", "url": task_url(notification.task)}]]
        })
    try:
        bot_api("sendMessage", **payload)
    except TelegramError:
        logger.exception("Could not send Telegram notification %s", notification.pk)
        return False
    return True


def webhook_url(request):
    return request.build_absolute_uri(reverse("telegram-webhook"))
