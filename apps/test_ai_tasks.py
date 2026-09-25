import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from .ai_provider import AIUnavailable, extract_task, transcribe
from .ai_tasks import create_ai_task
from .models import AITaskRequest, Department, Notification, Task, TelegramIntegration, User
from .telegram import TelegramError


class AITaskFixture:
    def setUp(self):
        self.department = Department.objects.create(name="IT", code="it")
        self.user = User.objects.create_user(username="manager", email="manager@example.com", role="manager", department=self.department)
        self.assignee = User.objects.create_user(username="muslima", email="muslima@example.com", first_name="Muslima", last_name="Zokirjonova", department=self.department)
        self.client.force_authenticate(self.user)
        self.payload = {"request_id": str(uuid.uuid4()), "text": "Muslima Zokirjonovaga websiteni fix qilsin deadline 23 may"}
        self.parsed = {
            "title": "Websiteni fix qilish", "description": "", "assignee": "Muslima Zokirjonova",
            "due_date": (timezone.localdate() + timedelta(days=30)).isoformat(),
            "priority": "medium", "project": None, "clarification": None,
        }
        self.extractor = patch("apps.ai_tasks.extract_task", return_value=self.parsed).start()
        self.addCleanup(patch.stopall)

    def post(self, payload=None):
        return self.client.post("/api/v1/ai/tasks/", payload or self.payload, format="json")


class AITaskTests(AITaskFixture, APITestCase):
    def test_creates_assignment_deadline_and_notification_once_on_retry(self):
        first = self.post()
        self.assertEqual(first.status_code, 201, first.data)
        second = self.post()
        self.assertEqual(first.data, second.data)
        task = Task.objects.get()
        self.assertEqual(task.main_assignee, self.assignee)
        self.assertEqual(task.created_by, self.user)
        self.assertEqual(str(task.due_date), self.parsed["due_date"])
        self.assertEqual(list(task.assignees.all()), [self.assignee])
        self.assertEqual(Notification.objects.filter(task=task).count(), 1)
        self.extractor.assert_called_once()

    def test_request_key_cannot_be_reused_for_different_content(self):
        self.post()
        self.payload["text"] = "Different task"
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(Task.objects.count(), 1)

    def test_ambiguous_name_creates_nothing(self):
        User.objects.create_user(username="other", email="other@example.com", first_name="Muslima", last_name="Zokirjonova", department=self.department)
        response = self.post()
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertFalse(Task.objects.exists())

    def test_unknown_person_and_project_and_past_date_create_nothing(self):
        for field, value in [("assignee", "Unknown Person"), ("project", "Unknown project"), ("due_date", "2000-05-23")]:
            with self.subTest(field=field):
                parsed = dict(self.parsed, **{field: value})
                self.extractor.return_value = parsed
                response = self.post(dict(self.payload, request_id=str(uuid.uuid4())))
                self.assertEqual(response.data["status"], "needs_clarification")
        self.assertFalse(Task.objects.exists())

    def test_member_cannot_assign_outside_accessible_departments(self):
        self.user.role = "member"
        self.user.save()
        other = Department.objects.create(name="Other", code="other")
        self.assignee.department = other
        self.assignee.save()
        self.assertEqual(self.post().data["status"], "needs_clarification")
        self.assertFalse(Task.objects.exists())

    def test_manager_can_assign_across_departments(self):
        other = Department.objects.create(name="Other", code="other")
        self.assignee.department = other
        self.assignee.save()
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(Task.objects.get().department, other)

    def test_requires_authentication_and_active_user(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.post().status_code, 401)
        self.user.is_active = False
        self.user.save()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.post().status_code, 403)
        self.extractor.assert_not_called()

    def test_provider_failure_does_not_leave_task_or_receipt(self):
        self.extractor.side_effect = AIUnavailable()
        self.assertEqual(self.post().status_code, 503)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(AITaskRequest.objects.exists())

    def test_clarification_from_model_never_creates_task(self):
        self.parsed["clarification"] = "Qaysi vazifani yaratish kerak?"
        self.assertEqual(self.post().data["status"], "needs_clarification")
        self.assertFalse(Task.objects.exists())

    @patch("apps.ai_tasks.transcribe", return_value="Muslima Zokirjonovaga websiteni fix qilish")
    def test_voice_uses_same_workflow(self, transcriber):
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.webm", b"audio", content_type="audio/webm"),
        }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["transcript"], transcriber.return_value)
        self.extractor.assert_called_once_with(transcriber.return_value)

    def test_rejects_unsupported_audio_and_text_with_audio(self):
        for filename, text in [("file.txt", ""), ("voice.ogg", "task")]:
            data = {"request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile(filename, b"audio")}
            if text:
                data["text"] = text
            self.assertEqual(self.client.post("/api/v1/ai/tasks/", data, format="multipart").status_code, 400)
        self.extractor.assert_not_called()


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret")
class TelegramAITaskTests(AITaskFixture, APITestCase):
    def setUp(self):
        super().setUp()
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=101, telegram_user_id=101, is_connected=True)
        self.bot = patch("apps.telegram_tasks.bot_api").start()
        self.message = {"message_id": 1, "chat": {"id": 101, "type": "private"}, "from": {"id": 101}, "text": self.payload["text"]}

    def webhook(self, secret="secret"):
        return self.client.post("/api/v1/telegram/webhook/", {"message": self.message}, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret)

    def test_telegram_text_and_replayed_update_create_one_task(self):
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(Task.objects.count(), 1)
        self.extractor.assert_called_once()

    def test_start_shows_create_button(self):
        self.message["text"] = "/start"
        self.webhook()
        self.assertIn("inline_keyboard", json.loads(self.bot.call_args.kwargs["reply_markup"]))
        self.assertIn("Create task", self.bot.call_args.kwargs["reply_markup"])
        self.assertTrue(json.loads(self.bot.call_args_list[0].kwargs["reply_markup"])["remove_keyboard"])
        self.extractor.assert_not_called()

    def test_menu_command_uses_one_telegram_request(self):
        self.message["text"] = "/menu"
        self.webhook()
        self.bot.assert_called_once()
        self.assertEqual(self.bot.call_args.args[0], "sendMessage")
        self.assertIn("TASKFLOW", self.bot.call_args.kwargs["text"])

    def test_inline_callback_shows_create_screen_without_creating_task(self):
        callback = {"id": "cb1", "from": {"id": 101}, "message": self.message, "data": "task:create"}
        response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                    format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
        self.assertEqual(response.status_code, 200)
        self.bot.assert_called_once_with("answerCallbackQuery", callback_query_id="cb1", timeout=3)
        self.assertEqual(response.data["method"], "sendMessage")
        self.assertEqual(response.data["parse_mode"], "HTML")
        self.assertIn("CREATE A TASK", response.data["text"])
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("inline_keyboard", response.data["reply_markup"])
        self.extractor.assert_not_called()
        self.assertFalse(Task.objects.exists())

    def test_inline_callback_rejects_unlinked_sender(self):
        callback = {"id": "cb2", "from": {"id": 999}, "message": self.message, "data": "task:create"}
        response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                    format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
        self.bot.assert_not_called()
        self.assertEqual(response.data["method"], "answerCallbackQuery")
        self.assertTrue(response.data["show_alert"])
        self.extractor.assert_not_called()

    @patch("apps.views.bot_api", return_value=True)
    def test_webhook_registration_includes_callbacks(self, bot):
        self.user.is_superuser = True
        self.user.save()
        response = self.client.post("/api/v1/telegram/setup-webhook/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("callback_query", json.loads(bot.call_args_list[0].kwargs["allowed_updates"]))
        self.assertEqual(bot.call_args_list[1].args[0], "setMyCommands")
        self.assertEqual(bot.call_args_list[2].args[0], "setChatMenuButton")
        self.assertEqual(json.loads(bot.call_args_list[2].kwargs["menu_button"]), {"type": "commands"})

    def test_all_menu_screens_navigate_without_ai(self):
        for screen, heading in [("menu", "TASKFLOW"), ("voice", "CREATE WITH YOUR VOICE"),
                                ("template", "TASK TEMPLATE"), ("example", "EXAMPLE TASK"),
                                ("help", "HOW TIKO WORKS")]:
            with self.subTest(screen=screen):
                callback = {"id": "cb", "from": {"id": 101}, "message": self.message,
                            "data": f"task:{screen}"}
                response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                            format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["method"], "sendMessage")
                self.assertIn(heading, response.data["text"])
        self.extractor.assert_not_called()
        self.assertFalse(Task.objects.exists())

    def test_template_command_and_repeated_navigation(self):
        self.message["text"] = "/template"
        self.webhook()
        self.assertIn("<pre>Create a task:", self.bot.call_args.kwargs["text"])
        self.assertIn("[Full name or email]", self.bot.call_args.kwargs["text"])
        callback = {"id": "cb", "from": {"id": 101}, "message": self.message, "data": "task:template"}
        for _ in range(2):
            response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                        format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["method"], "sendMessage")
            self.assertIn("TASK TEMPLATE", response.data["text"])
        self.assertEqual([call.args[0] for call in self.bot.call_args_list[-2:]],
                         ["answerCallbackQuery", "answerCallbackQuery"])
        self.extractor.assert_not_called()

    def test_expired_callback_still_sends_selected_screen(self):
        def bot_result(method, **kwargs):
            if method == "answerCallbackQuery":
                raise TelegramError("query is too old")
        self.bot.side_effect = bot_result
        callback = {"id": "old", "from": {"id": 101}, "message": self.message, "data": "task:example"}
        response = self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                    format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["method"], "sendMessage")
        self.assertIn("EXAMPLE TASK", response.data["text"])

    def test_task_confirmation_escapes_html(self):
        self.parsed["title"] = "Fix <login> & signup"
        self.webhook()
        self.assertIn("Fix &lt;login&gt; &amp; signup", self.bot.call_args.kwargs["text"])
        self.assertIn("TASK CREATED", self.bot.call_args.kwargs["text"])

    def test_unlinked_sender_and_group_cannot_create(self):
        self.message["from"]["id"] = 999
        self.webhook()
        self.message["from"]["id"] = 101
        self.message["chat"]["type"] = "group"
        self.webhook()
        self.assertFalse(Task.objects.exists())
        self.extractor.assert_not_called()

    def test_bad_secret(self):
        self.assertEqual(self.webhook(secret="wrong").status_code, 403)
        self.bot.assert_not_called()

    def test_reply_failure_can_retry_without_duplicate_task(self):
        self.bot.side_effect = TelegramError("Unavailable")
        self.assertEqual(self.webhook().status_code, 502)
        self.bot.side_effect = None
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(Task.objects.count(), 1)
        self.extractor.assert_called_once()

    def test_group_cannot_consume_connection_token(self):
        integration = TelegramIntegration.objects.get(user=self.user)
        integration.link_token = "private-token"
        integration.link_token_expires_at = timezone.now() + timedelta(minutes=15)
        integration.save()
        self.message["text"] = "/start private-token"
        self.message["chat"]["type"] = "group"
        self.webhook()
        integration.refresh_from_db()
        self.assertEqual(integration.link_token, "private-token")

    @patch("apps.telegram_tasks.download_voice")
    @patch("apps.ai_tasks.transcribe", return_value="Muslima Zokirjonovaga websiteni fix qilish")
    def test_telegram_voice_downloads_only_once_on_replay(self, transcriber, download):
        download.return_value = SimpleUploadedFile("voice.ogg", b"audio")
        self.message.pop("text")
        self.message["voice"] = {"file_id": "abc", "file_unique_id": "unique"}
        self.webhook()
        self.webhook()
        self.assertEqual(Task.objects.count(), 1)
        download.assert_called_once()
        transcriber.assert_called_once()


@override_settings(OPENAI_API_KEY="test", OPENAI_TASK_MODEL="gpt-5.4-mini", OPENAI_TRANSCRIPTION_MODEL="gpt-4o-mini-transcribe")
class AIProviderTests(SimpleTestCase):
    @patch("apps.ai_provider.request_ai")
    def test_refusal_or_malformed_output_is_not_a_task(self, provider):
        for response in [{}, {"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}, {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}]:
            provider.return_value = response
            with self.assertRaises(AIUnavailable):
                extract_task("create a task")

    @patch("apps.ai_provider.request_ai", return_value={"text": "Muslima uchun task"})
    def test_audio_is_uploaded_with_safe_filename(self, provider):
        self.assertEqual(transcribe(SimpleUploadedFile("voice.ogg", b"sample audio")), "Muslima uchun task")
        args = provider.call_args.args
        self.assertEqual(args[0], "audio/transcriptions")
        self.assertIn(b'filename="voice.ogg"', args[1])
        self.assertIn(b"sample audio", args[1])


class ConcurrentAITaskTests(AITaskFixture, APITransactionTestCase):
    def test_concurrent_retries_create_one_task_and_notification(self):
        def submit():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                return create_ai_task(user=user, request_key="web:concurrent", text=self.payload["text"])
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)
        self.extractor.assert_called_once()
