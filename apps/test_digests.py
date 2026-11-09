from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .digests import send_daily_digests, send_weekly_performance, today_html
from .models import Department, Notification, Task, TelegramIntegration, User, UserPreference

TODAY = date(2026, 11, 9)  # Monday


class DigestFixture:
    def setUp(self):
        self.department = Department.objects.create(name="IT", code="it")
        self.user = User.objects.create_user(username="ali", email="ali@example.com", first_name="Ali",
                                             last_name="Valiyev", department=self.department)
        self.boss = User.objects.create_user(username="boss", email="boss@example.com", role="manager",
                                             department=self.department)
        self.telegram = patch("apps.telegram.bot_api").start()
        self.addCleanup(patch.stopall)

    def task(self, title, due, status=Task.Status.NOT_STARTED, completed=None, assignee=None):
        task = Task.objects.create(department=self.department, title=title, due_date=due, status=status,
                                   created_by=self.boss, main_assignee=assignee or self.user)
        task.assignees.add(assignee or self.user)
        if completed:
            Task.objects.filter(pk=task.pk).update(
                completed_at=timezone.make_aware(datetime.combine(completed, datetime.min.time())))
        return task


class DailyDigestTests(DigestFixture, TestCase):
    def test_digest_lists_overdue_first_and_is_sent_once(self):
        self.task("Write report", TODAY)
        self.task("Fix login", TODAY - timedelta(days=3))
        self.task("Plan sprint", TODAY + timedelta(days=4))
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=5, telegram_user_id=5, is_connected=True)

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_daily_digests(TODAY), 1)
            self.assertEqual(send_daily_digests(TODAY), 0)

        notification = Notification.objects.get(recipient=self.user, notification_type="daily_digest")
        self.assertEqual(notification.title, "Today’s plan · 1 overdue")
        lines = notification.body.splitlines()
        self.assertEqual(lines[0], "Today: 1 · Overdue: 1 · Open: 3")
        self.assertEqual(lines[2], "🔴 Fix login — 3d late")
        self.assertEqual(lines[3], "📅 Write report — today")
        self.telegram.assert_called_once()
        self.assertIn("Today’s plan", self.telegram.call_args.kwargs["text"])
        self.assertIn("Open TaskFlow", self.telegram.call_args.kwargs["reply_markup"])

    def test_no_open_tasks_or_disabled_preference_sends_nothing(self):
        self.task("Done", TODAY, status=Task.Status.COMPLETED, completed=TODAY)
        self.assertEqual(send_daily_digests(TODAY), 0)
        self.task("Open", TODAY)
        UserPreference.objects.update_or_create(user=self.user, defaults={"daily_digest": False})
        self.assertEqual(send_daily_digests(TODAY), 0)
        self.assertFalse(Notification.objects.filter(notification_type="daily_digest").exists())

    def test_today_html_escapes_titles(self):
        self.task("Fix <b> & co", timezone.localdate())
        html = today_html(self.user)
        self.assertIn("Fix &lt;b&gt; &amp; co", html)
        self.assertIn("<b>Today’s plan</b>", html)
        Task.objects.update(status=Task.Status.COMPLETED)
        self.assertIn("No open tasks", today_html(self.user))


class WeeklyPerformanceTests(DigestFixture, TestCase):
    def test_score_uses_analytics_formula_and_suggests_improvement(self):
        last_monday = TODAY - timedelta(days=7)
        # 2 accountable tasks: one completed on time, one still overdue.
        self.task("On time", last_monday + timedelta(days=2), Task.Status.COMPLETED, completed=last_monday + timedelta(days=1))
        self.task("Late", last_monday + timedelta(days=3), Task.Status.IN_PROGRESS)
        # Not accountable (not started) and outside the week: ignored.
        self.task("Ignored", last_monday + timedelta(days=3))
        self.task("Next week", TODAY + timedelta(days=1), Task.Status.IN_PROGRESS)

        self.assertEqual(send_weekly_performance(TODAY), 1)
        self.assertEqual(send_weekly_performance(TODAY), 0)
        notification = Notification.objects.get(notification_type="weekly_performance")
        # completion 50 * .45 + on-time 100 * .35 + overdue control 50 * .20 = 68
        self.assertEqual(notification.title, "Your week: 68/100 · Needs improvement")
        self.assertIn("✅ Done: 1 of 2", notification.body)
        self.assertIn("Let’s improve", notification.body)

    def test_users_without_accountable_tasks_are_skipped(self):
        self.assertEqual(send_weekly_performance(TODAY), 0)


class DigestCommandTests(DigestFixture, TestCase):
    @patch("apps.management.commands.send_digests.timezone.localdate", return_value=TODAY)
    def test_command_sends_daily_and_monday_weekly(self, _):
        self.task("Today", TODAY)
        self.task("Last week", TODAY - timedelta(days=5), Task.Status.COMPLETED, completed=TODAY - timedelta(days=6))
        call_command("send_digests", stdout=StringIO())
        types = set(Notification.objects.values_list("notification_type", flat=True))
        self.assertTrue({"daily_digest", "weekly_performance"} <= types)


class NotificationPolicyTests(DigestFixture, TestCase):
    def test_deadline_reminder_is_on_by_default_and_only_day_before(self):
        from .notifications import generate_deadline_notifications
        self.task("Tomorrow", TODAY + timedelta(days=1))
        self.task("In three days", TODAY + timedelta(days=3))
        self.assertEqual(generate_deadline_notifications(TODAY), 1)
        self.assertEqual(Notification.objects.get(notification_type="deadline_reminder").title, "Deadline tomorrow")

    def test_chat_messages_are_not_pushed_to_telegram(self):
        from .notifications import create_notification
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=5, telegram_user_id=5, is_connected=True)
        with self.captureOnCommitCallbacks(execute=True):
            create_notification(recipient=self.user, notification_type="new_message", title="New message", body="hi")
            create_notification(recipient=self.user, notification_type="task_overdue", title="Task overdue")
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 2)
        self.telegram.assert_called_once()
        self.assertIn("Task overdue", self.telegram.call_args.kwargs["text"])


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret")
class TelegramTodayTests(DigestFixture, APITestCase):
    def test_all_employees_confirm_button_creates_tasks(self):
        from .models import AITaskRequest
        TelegramIntegration.objects.create(user=self.boss, telegram_chat_id=8, telegram_user_id=8, is_connected=True)
        AITaskRequest.objects.create(user=self.boss, request_key="telegram:8:1", fingerprint="x", result={
            "status": "needs_confirmation", "action": "assign_all", "confirmation_code": "ABCDEF123456",
            "transcript": "all", "departments": ["IT"], "member_ids": [str(self.user.pk)],
            "pending_task": {"title": "Update statuses", "description": "", "assignee": "all", "project": None,
                             "due_date": None, "priority": "medium"},
        })
        message = {"message_id": 2, "chat": {"id": 8, "type": "private"}, "from": {"id": 8}}
        callback = {"id": "cb", "from": {"id": 8}, "message": message, "data": "task:all:ABCDEF123456"}
        with patch("apps.telegram_tasks.bot_api"):
            response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                        format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
        self.assertIn("👥 1 employees", response.data["text"])
        self.assertEqual(Task.objects.get().main_assignee, self.user)

    def test_today_button_and_command(self):
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=7, telegram_user_id=7, is_connected=True)
        self.task("Fix login", timezone.localdate())
        message = {"message_id": 1, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}}
        with patch("apps.telegram_tasks.bot_api") as bot:
            callback = {"id": "cb", "from": {"id": 7}, "message": message, "data": "task:today"}
            response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                        format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
            self.assertIn("Fix login", response.data["text"])
            response = self.client.post("/api/v1/telegram/webhook/", {"message": {**message, "text": "/today"}},
                                        format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
            self.assertIn("Fix login", response.data["text"])
