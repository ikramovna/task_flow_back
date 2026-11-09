import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import Notification, Task, UserPreference

logger = logging.getLogger(__name__)
_telegram_pool = None


def _preference(user):
    preference, _ = UserPreference.objects.get_or_create(user=user)
    return preference


def create_notification(*, recipient, notification_type, title, body="", actor=None, task=None, message=None, event_key=None):
    if not recipient.is_active:
        return None
    values = {
        "actor": actor,
        "notification_type": notification_type,
        "title": title,
        "body": body,
        "task": task,
        "message": message,
    }
    if event_key:
        notification, was_created = Notification.objects.get_or_create(
            recipient=recipient, event_key=event_key, defaults=values
        )
        if was_created:
            transaction.on_commit(lambda: _dispatch_telegram(notification.pk))
        return notification
    notification = Notification.objects.create(recipient=recipient, **values)
    transaction.on_commit(lambda: _dispatch_telegram(notification.pk))
    return notification


def _dispatch_telegram(notification_id):
    """Each Telegram call can take seconds; never make the bot reply or a web request wait for it."""
    global _telegram_pool
    if not settings.TELEGRAM_NOTIFY_IN_BACKGROUND:
        _send_telegram(notification_id)
        return
    if _telegram_pool is None:
        _telegram_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="telegram-notify")
    _telegram_pool.submit(_send_telegram_in_thread, notification_id)


def _send_telegram_in_thread(notification_id):
    try:
        _send_telegram(notification_id)
    except Exception:
        logger.exception("Could not push notification %s to Telegram", notification_id)
    finally:
        connection.close()  # the worker thread's own connection


# Chat messages stay in the web app: pushing every message would bury task notifications.
TELEGRAM_SKIPPED_TYPES = {Notification.Type.NEW_MESSAGE}


def _send_telegram(notification_id):
    from .telegram import send_notification

    notification = Notification.objects.select_related("recipient", "actor", "task").get(pk=notification_id)
    if notification.notification_type in TELEGRAM_SKIPPED_TYPES:
        return
    send_notification(notification)


def notify_task_assigned(task, actor, recipients):
    for recipient in recipients:
        if recipient != actor and _preference(recipient).task_assigned:
            create_notification(
                recipient=recipient,
                actor=actor,
                notification_type=Notification.Type.TASK_ASSIGNED,
                title="New task assigned",
                body=task.title,
                task=task,
            )


def notify_task_completed(task, actor):
    recipient = task.created_by
    if recipient != actor and _preference(recipient).task_updates:
        create_notification(
            recipient=recipient,
            actor=actor,
            notification_type=Notification.Type.TASK_COMPLETED,
            title="Task completed",
            body=task.title,
            task=task,
        )


def notify_new_message(message):
    links = message.conversation.participant_links.select_related("user", "user__preferences")
    for link in links:
        recipient = link.user
        if recipient == message.sender or link.is_muted:
            continue
        if _preference(recipient).team_messages:
            create_notification(
                recipient=recipient,
                actor=message.sender,
                notification_type=Notification.Type.NEW_MESSAGE,
                title="New message",
                body=message.body[:180] if message.body else "Attachment received",
                message=message,
            )


def generate_deadline_notifications(today=None):
    today = today or timezone.localdate()
    created = 0
    tasks = Task.objects.exclude(status=Task.Status.COMPLETED).filter(
        is_archived=False, due_date__isnull=False
    ).select_related("created_by").prefetch_related("assignees")
    for task in tasks:
        days = (task.due_date - today).days
        # Only the day before: the morning "Today's plan" already lists upcoming tasks.
        if days == 1:
            notification_type = Notification.Type.DEADLINE_REMINDER
            preference_name = "deadline_reminder"
            title = "Deadline tomorrow"
            event_key = f"task:{task.pk}:deadline:{task.due_date}:d{days}"
        elif days < 0:
            notification_type = Notification.Type.TASK_OVERDUE
            preference_name = "task_overdue"
            title = "Task overdue"
            event_key = f"task:{task.pk}:overdue:{task.due_date}"
        else:
            continue
        for recipient in task.assignees.all():
            if getattr(_preference(recipient), preference_name):
                before = Notification.objects.filter(recipient=recipient, event_key=event_key).exists()
                create_notification(
                    recipient=recipient,
                    notification_type=notification_type,
                    title=title,
                    body=task.title,
                    task=task,
                    event_key=event_key,
                )
                created += int(not before)
    return created
