"""Daily task digest and weekly performance push notifications."""
from datetime import timedelta
from html import escape

from django.db.models import Q
from django.utils import timezone

from .models import Notification, Task, User
from .notifications import _preference, create_notification

TOP_TASKS = 5
# Same accountability rule and weights as the Analytics staff performance table.
ACCOUNTABLE_STATUSES = (Task.Status.IN_PROGRESS, Task.Status.COMPLETED)


def _create_once(user, event_key, **values):
    """Cron may run twice; the unique event key keeps each digest to one push."""
    if Notification.objects.filter(recipient=user, event_key=event_key).exists():
        return False
    return bool(create_notification(recipient=user, event_key=event_key, **values))


def _my_tasks(user):
    return Task.objects.filter(Q(assignees=user) | Q(main_assignee=user)).distinct()


def _open_tasks(user):
    return _my_tasks(user).filter(is_archived=False).exclude(status=Task.Status.COMPLETED)


def today_summary(user, today=None):
    today = today or timezone.localdate()
    tasks = list(_open_tasks(user).only("id", "title", "due_date", "priority"))
    overdue = sorted((t for t in tasks if t.due_date and t.due_date < today), key=lambda t: t.due_date)
    due_today = [t for t in tasks if t.due_date == today]
    upcoming = sorted((t for t in tasks if t.due_date and t.due_date > today), key=lambda t: t.due_date)
    undated = [t for t in tasks if not t.due_date]
    focus = (overdue + due_today + upcoming + undated)[:TOP_TASKS]
    return {"today": today, "open": len(tasks), "overdue": overdue, "due_today": due_today, "focus": focus}


def _task_line(task, today):
    if not task.due_date:
        return f"• {task.title}"
    days = (task.due_date - today).days
    if days < 0:
        return f"🔴 {task.title} — {-days}d late"
    if days == 0:
        return f"📅 {task.title} — today"
    return f"• {task.title} — {task.due_date:%d %b}"


def _summary_lines(summary):
    lines = [f"Today: {len(summary['due_today'])} · Overdue: {len(summary['overdue'])} · Open: {summary['open']}"]
    if summary["focus"]:
        lines.append("")
        lines += [_task_line(task, summary["today"]) for task in summary["focus"]]
        more = summary["open"] - len(summary["focus"])
        if more > 0:
            lines.append(f"+{more} more")
    return lines


def today_html(user):
    summary = today_summary(user)
    if not summary["open"]:
        return "🎉 <b>No open tasks.</b> Enjoy your day!"
    rest = _summary_lines(summary)[1:]
    counts = (f"📅 Today <b>{len(summary['due_today'])}</b> · 🔴 Overdue <b>{len(summary['overdue'])}</b> · "
              f"Open <b>{summary['open']}</b>")
    return "☀️ <b>Today’s plan</b>\n\n" + counts + "\n" + escape("\n".join(rest))


def send_daily_digests(today=None):
    today = today or timezone.localdate()
    sent = 0
    for user in User.objects.filter(is_active=True):
        if not _preference(user).daily_digest:
            continue
        summary = today_summary(user, today)
        if not summary["open"]:
            continue
        title = "Today’s plan"
        if summary["overdue"]:
            title += f" · {len(summary['overdue'])} overdue"
        sent += _create_once(
            user, f"digest:daily:{today.isoformat()}", notification_type=Notification.Type.DAILY_DIGEST,
            title=title, body="\n".join(_summary_lines(summary)),
        )
    return sent


def _pct(value, total):
    return round(value * 100 / total, 1) if total else 0.0


def performance(user, start, end):
    tasks = [task for task in _my_tasks(user).filter(
        status__in=ACCOUNTABLE_STATUSES, due_date__gte=start, due_date__lte=end,
    ).only("due_date", "completed_at")]
    if not tasks:
        return None
    completed = [t for t in tasks if t.completed_at and t.completed_at.date() <= end]
    on_time = sum(t.completed_at.date() <= t.due_date for t in completed)
    overdue = sum(t.due_date < end and (not t.completed_at or t.completed_at.date() > end) for t in tasks)
    completion_rate = _pct(len(completed), len(tasks))
    on_time_rate = _pct(on_time, len(completed))
    overdue_control = 100.0 - _pct(overdue, len(tasks))
    return {
        "score": round(completion_rate * .45 + on_time_rate * .35 + overdue_control * .20),
        "total": len(tasks), "completed": len(completed), "on_time_rate": round(on_time_rate),
        "overdue": overdue,
    }


def performance_level(score):
    if score >= 90:
        return "Outstanding", "Outstanding work — keep it up! 🚀"
    if score >= 80:
        return "Excellent", "Great week. Keep the pace! 💪"
    if score >= 70:
        return "Good", "Good job. Close overdue tasks to reach 80+."
    if score >= 60:
        return "Needs improvement", "Let’s improve: finish overdue tasks first and plan realistic deadlines."
    return "Critical", "Let’s improve together: start with the oldest overdue task today."


def send_weekly_performance(today=None):
    today = today or timezone.localdate()
    start = today - timedelta(days=today.weekday() + 7)  # previous Monday
    end = start + timedelta(days=6)
    sent = 0
    for user in User.objects.filter(is_active=True):
        if not _preference(user).weekly_performance:
            continue
        result = performance(user, start, end)
        if not result:
            continue
        level, tip = performance_level(result["score"])
        lines = [
            f"✅ Done: {result['completed']} of {result['total']}",
            f"⏱ On time: {result['on_time_rate']}%",
            f"🔴 Overdue: {result['overdue']}",
        ]
        previous = performance(user, start - timedelta(days=7), start - timedelta(days=1))
        if previous:
            delta = result["score"] - previous["score"]
            lines.append(f"{'▲' if delta >= 0 else '▼'} {delta:+d} vs previous week")
        lines += ["", tip]
        sent += _create_once(
            user, f"digest:weekly:{start.isoformat()}", notification_type=Notification.Type.WEEKLY_PERFORMANCE,
            title=f"Your week: {result['score']}/100 · {level}", body="\n".join(lines),
        )
    return sent
