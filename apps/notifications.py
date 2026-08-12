from django.db import transaction
from django.utils import timezone

from .models import Notification, Task, UserPreference


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
            transaction.on_commit(lambda: _send_telegram(notification.pk))
        return notification
    notification = Notification.objects.create(recipient=recipient, **values)
    transaction.on_commit(lambda: _send_telegram(notification.pk))
    return notification


def _send_telegram(notification_id):
    from .telegram import send_notification

    notification = Notification.objects.select_related("recipient", "actor", "task").get(pk=notification_id)
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
        if days in (1, 3):
            notification_type = Notification.Type.DEADLINE_REMINDER
            preference_name = "deadline_reminder"
            title = f"Task deadline in {days} day" + ("s" if days != 1 else "")
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
