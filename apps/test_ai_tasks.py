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
from .ai_tasks import create_ai_task, task_intent
from .models import AITaskRequest, Department, Notification, Task, TelegramIntegration, User
from .telegram import TelegramError, task_url


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


class TaskIntentTests(SimpleTestCase):
    def test_uzbek_voice_edit_is_routed_to_update(self):
        self.assertEqual(task_intent("Shu vazifani to'g'lab qo'y"), "update")
        self.assertEqual(task_intent("Hozir yaratgan taskni o'chir"), "delete")

    def test_new_task_to_fix_or_remove_something_stays_creation(self):
        self.assertEqual(task_intent("Fix website login page"), "create")
        self.assertEqual(task_intent("Remove old icons from website"), "create")
        self.assertEqual(task_intent("Create a task: update task list dashboard"), "create")
        self.assertEqual(task_intent("Please update your Taskflow task status on time. All employees"), "create")
        self.assertEqual(task_intent("Обновите свои задачи до пятницы"), "create")
        self.assertEqual(task_intent("Edit task: set priority high"), "update")


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

    def ask_without_assignee(self, assignee="self"):
        self.payload["text"] = "mept update\n\ndue today"
        self.parsed["assignee"] = assignee
        response = self.post()
        self.assertEqual(response.data["status"], "needs_clarification", response.data)
        self.assertEqual(response.data["clarification_type"], "assignee")
        self.assertFalse(Task.objects.exists())
        return response

    def reply(self, text):
        return self.post({"request_id": str(uuid.uuid4()), "text": text})

    def test_self_assignee_without_self_reference_asks_who(self):
        self.ask_without_assignee("self")

    def test_missing_assignee_reply_with_name_creates_saved_task(self):
        self.ask_without_assignee("")
        response = self.reply("Muslima")
        self.assertEqual(response.status_code, 201, response.data)
        task = Task.objects.get()
        self.assertEqual(task.main_assignee, self.assignee)
        self.assertEqual(task.title, self.parsed["title"])
        self.extractor.assert_called_once()

    def test_missing_assignee_reply_menga_assigns_requester(self):
        self.ask_without_assignee("")
        response = self.reply("menga")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee, self.user)

    def test_missing_assignee_reply_with_shared_name_asks_which(self):
        User.objects.create_user(username="other", email="other@example.com", first_name="Muslima",
                                 last_name="Karimova", department=self.department)
        self.ask_without_assignee("")
        response = self.reply("Muslima")
        self.assertEqual(len(response.data["candidates"]), 2, response.data)
        response = self.reply("Karimova")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee.last_name, "Karimova")

    def test_missing_assignee_unknown_reply_keeps_asking(self):
        self.ask_without_assignee("")
        response = self.reply("Nobody")
        self.assertEqual(response.data["clarification_type"], "assignee", response.data)
        self.assertFalse(Task.objects.exists())

    def test_explicit_self_assignment_is_allowed(self):
        self.payload["text"] = "Menga websiteni fix qilish taskini yarat, deadline 23 may"
        self.parsed["assignee"] = "self"
        response = self.post()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee, self.user)

    def ask_all(self):
        self.payload["text"] = "1. Update your Taskflow task status on time.\n2. All employee's\n3. Till 11.11.2027."
        self.parsed["assignee"] = "All employee's"
        return self.post()

    def test_all_employees_asks_then_creates_one_task_each_in_own_departments(self):
        sales = Department.objects.create(name="Sales", code="sales")
        other = Department.objects.create(name="Other", code="other")
        self.user.accessible_departments.add(sales)
        seller = User.objects.create_user(username="s", email="s@example.com", department=sales)
        outsider = User.objects.create_user(username="o", email="o@example.com", department=other)
        User.objects.create_user(username="gone", email="g@example.com", department=self.department, is_active=False)

        asked = self.ask_all()
        self.assertEqual(asked.data["status"], "needs_confirmation", asked.data)
        self.assertEqual(asked.data["action"], "assign_all")
        self.assertIn("2 employees (IT, Sales)", asked.data["message"])
        self.assertFalse(Task.objects.exists())

        done = self.reply(f"CONFIRM ALL {asked.data['confirmation_code']}")
        self.assertEqual(done.status_code, 201, done.data)
        self.assertEqual(done.data["count"], 2)
        owners = {task.main_assignee for task in Task.objects.all()}
        self.assertEqual(owners, {self.assignee, seller})
        self.assertNotIn(outsider, owners)
        self.assertNotIn(self.user, owners)
        self.assertEqual(Notification.objects.filter(notification_type="task_assigned").count(), 2)
        # The code cannot be reused.
        self.assertEqual(self.reply(f"CONFIRM ALL {asked.data['confirmation_code']}").data["status"], "needs_clarification")
        self.assertEqual(Task.objects.count(), 2)

    def test_member_cannot_assign_to_all(self):
        self.user.role = "member"
        self.user.save(update_fields=["role"])
        response = self.ask_all()
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertIn("Owner, Admin, or Manager", response.data["message"])
        self.assertFalse(Task.objects.exists())

    def test_all_without_all_words_in_request_is_not_trusted(self):
        self.payload["text"] = "Fix the website by Friday"
        self.parsed["assignee"] = "all"
        response = self.post()
        self.assertNotEqual(response.data.get("action"), "assign_all")
        self.assertFalse(Task.objects.exists())

    def test_first_name_creates_task_when_only_one_employee_matches(self):
        User.objects.create_user(username="surname-match", email="surname@example.com",
                                 first_name="Ali", last_name="Muslima", department=self.department)
        self.parsed["assignee"] = "Muslima"
        response = self.post()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee, self.assignee)

    def test_duplicate_first_name_asks_which_employee_then_accepts_surname(self):
        other = User.objects.create_user(username="second", email="second@example.com",
                                         first_name="Muslima", last_name="Karimova", department=self.department)
        self.parsed["assignee"] = "Muslima"
        first = self.post()
        self.assertEqual(first.data["status"], "needs_clarification")
        self.assertEqual(first.data["clarification_type"], "assignee")
        self.assertIn("Which Muslima?", first.data["message"])
        self.assertIn("Muslima Zokirjonova", first.data["message"])
        self.assertIn("Muslima Karimova", first.data["message"])
        self.assertFalse(Task.objects.exists())

        still_ambiguous = self.post({"request_id": str(uuid.uuid4()), "text": "Muslima"})
        self.assertEqual(still_ambiguous.data["clarification_type"], "assignee")
        self.assertFalse(Task.objects.exists())

        request_id = str(uuid.uuid4())
        answer = {"request_id": request_id, "text": "Karimova"}
        created = self.post(answer)
        self.assertEqual(created.status_code, 201, created.data)
        task = Task.objects.get()
        self.assertEqual(task.main_assignee, other)
        self.assertEqual(task.title, self.parsed["title"])
        self.assertEqual(Notification.objects.filter(task=task).count(), 1)
        self.assertEqual(self.post(answer).data, created.data)
        self.extractor.assert_called_once()

    def test_duplicate_full_names_can_be_selected_by_email(self):
        other = User.objects.create_user(username="second", email="second@example.com",
                                         first_name="Muslima", last_name="Zokirjonova", department=self.department)
        self.parsed["assignee"] = "Muslima"
        self.assertEqual(self.post().data["clarification_type"], "assignee")
        created = self.post({"request_id": str(uuid.uuid4()), "text": other.email})
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(Task.objects.get().main_assignee, other)

    def test_expired_name_question_does_not_use_old_task_details(self):
        User.objects.create_user(username="second", email="second@example.com",
                                 first_name="Muslima", last_name="Karimova", department=self.department)
        self.parsed["assignee"] = "Muslima"
        self.assertEqual(self.post().data["clarification_type"], "assignee")
        AITaskRequest.objects.update(created_at=timezone.now() - timedelta(minutes=11))
        response = self.post({"request_id": str(uuid.uuid4()), "text": "Karimova"})
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

    @patch("apps.ai_tasks.transcribe", return_value="Muslima Zakirjonovaga saytni tuzatish")
    def test_voice_recovers_one_surname_transcription_error(self, transcriber):
        self.parsed["assignee"] = "Muslima Zakirjonova"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee, self.assignee)

    @patch("apps.ai_tasks.transcribe", return_value="Muslama Zokirjonovaga saytni tuzatish")
    def test_voice_recovers_one_first_name_transcription_error(self, transcriber):
        self.parsed["assignee"] = "Muslama Zokirjonova"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee, self.assignee)

    @patch("apps.ai_tasks.transcribe", return_value="Muslima Zakirjanovaga saytni tuzatish")
    def test_voice_recovers_two_surname_letters_only_when_unique(self, transcriber):
        self.parsed["assignee"] = "Muslima Zakirjanova"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get().main_assignee, self.assignee)

    def test_text_matches_cyrillic_spelling_and_uzbek_case_ending(self):
        for spoken in ("Муслима Зокиржонова", "Muslima Zokirjonovaga"):
            with self.subTest(spoken=spoken):
                self.parsed["assignee"] = spoken
                payload = {"request_id": str(uuid.uuid4()), "text": self.payload["text"]}
                response = self.post(payload)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(Task.objects.latest("created_at").main_assignee, self.assignee)

    @patch("apps.ai_tasks.transcribe", return_value="Muslima Zikirjonovaga saytni tuzatish")
    def test_voice_near_match_rejects_two_possible_assignees(self, transcriber):
        User.objects.create_user(username="similar", email="similar@example.com",
                                 first_name="Muslima", last_name="Zakirjonova", department=self.department)
        self.parsed["assignee"] = "Muslima Zikirjonova"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertFalse(Task.objects.exists())

    @patch("apps.ai_tasks.transcribe", return_value="Muslama Zakirjonovaga saytni tuzatish")
    def test_voice_does_not_guess_when_both_name_parts_are_wrong(self, transcriber):
        self.parsed["assignee"] = "Muslama Zakirjonova"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertFalse(Task.objects.exists())

    @patch("apps.ai_tasks.transcribe", return_value="Muslima Zakirovaga saytni tuzatish")
    def test_uploaded_audio_clarification_reports_heard_name_and_transcript(self, transcriber):
        self.parsed["assignee"] = "Muslima Zakirova"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertIn("Muslima Zakirova", response.data["message"])
        self.assertIn("I heard: Muslima Zakirovaga saytni tuzatish", response.data["message"])
        self.assertIn("Possible matches: Muslima Zokirjonova", response.data["message"])
        self.assertFalse(Task.objects.exists())

    def test_rejects_unsupported_audio_and_text_with_audio(self):
        for filename, text in [("file.txt", ""), ("voice.ogg", "task")]:
            data = {"request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile(filename, b"audio")}
            if text:
                data["text"] = text
            self.assertEqual(self.client.post("/api/v1/ai/tasks/", data, format="multipart").status_code, 400)
        self.extractor.assert_not_called()


class AITaskChangeTests(AITaskFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.changer = patch("apps.ai_tasks.extract_task_change").start()
        self.changer.return_value = {
            "action": "update", "target": "last", "title": None, "description": None,
            "assignee": None, "project": None, "due_date": None, "priority": None,
            "status": None, "clarification": None,
        }
        self.assertEqual(self.post().status_code, 201)

    def change(self, text, request_id=None):
        return self.post({"request_id": request_id or str(uuid.uuid4()), "text": text})

    def test_edits_last_created_task_without_creating_another(self):
        task = Task.objects.get()
        self.changer.return_value.update(title="Fix the login page", priority="high")
        response = self.change("Edit my last created task: rename it and set high priority")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "updated")
        self.assertEqual(response.data["task"]["id"], str(task.pk))
        task.refresh_from_db()
        self.assertEqual(task.title, "Fix the login page")
        self.assertEqual(task.priority, "high")
        self.assertEqual(Task.objects.count(), 1)

    @patch("apps.ai_tasks.transcribe", return_value="Oxirgi yaratgan taskni edit qil, priority high")
    def test_uploaded_voice_can_edit_last_created_task(self, transcriber):
        self.changer.return_value["priority"] = "high"
        response = self.client.post("/api/v1/ai/tasks/", {
            "request_id": str(uuid.uuid4()), "audio": SimpleUploadedFile("voice.ogg", b"audio"),
        }, format="multipart")
        self.assertEqual(response.data["status"], "updated")
        self.assertEqual(Task.objects.get().priority, "high")

    def test_delete_requires_confirmation_and_is_idempotent(self):
        task_id = Task.objects.get().pk
        self.changer.return_value.update(action="delete")
        first = self.change("Delete my last created task")
        self.assertEqual(first.data["status"], "needs_confirmation")
        self.assertTrue(Task.objects.filter(pk=task_id).exists())
        code = first.data["confirmation_code"]
        request_id = str(uuid.uuid4())
        confirmed = self.change(f"CONFIRM DELETE {code}", request_id)
        self.assertEqual(confirmed.data["status"], "deleted")
        self.assertFalse(Task.objects.filter(pk=task_id).exists())
        self.assertEqual(self.change(f"CONFIRM DELETE {code}", request_id).data, confirmed.data)

    def test_delete_confirmation_expires_without_deleting(self):
        self.changer.return_value.update(action="delete")
        pending = self.change("Delete my last created task")
        code = pending.data["confirmation_code"]
        receipt = AITaskRequest.objects.filter(result__status="needs_confirmation").get()
        AITaskRequest.objects.filter(pk=receipt.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        response = self.change(f"CONFIRM DELETE {code}")
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertTrue(Task.objects.exists())

    def test_update_never_selects_another_users_task(self):
        other = User.objects.create_user(username="other", email="other@example.com", role="manager",
                                         department=self.department)
        task = Task.objects.get()
        self.changer.return_value.update(target=str(task.pk), priority="high")
        self.client.force_authenticate(other)
        response = self.change("Edit task: set priority high")
        self.assertEqual(response.data["status"], "needs_clarification")
        task.refresh_from_db()
        self.assertEqual(task.priority, "medium")

    def test_last_task_does_not_fall_back_to_older_task_after_delete(self):
        older_task_id = Task.objects.get().pk
        self.payload["request_id"] = str(uuid.uuid4())
        self.assertEqual(self.post().status_code, 201)
        newer_task_id = Task.objects.exclude(pk=older_task_id).get().pk
        Task.objects.filter(pk=newer_task_id).delete()
        self.changer.return_value["priority"] = "high"
        response = self.change("Edit my last created task: priority high")
        self.assertEqual(response.data["status"], "needs_clarification")
        self.assertEqual(Task.objects.get(pk=older_task_id).priority, "medium")

    def test_member_cannot_edit_or_delete_via_ai(self):
        self.user.role = "member"
        self.user.save(update_fields=["role"])
        self.changer.return_value["priority"] = "high"
        self.assertEqual(self.change("Edit my last created task: high priority").status_code, 403)
        self.changer.return_value.update(action="delete")
        self.assertEqual(self.change("Delete my last created task").status_code, 403)
        self.assertTrue(Task.objects.exists())


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret")
class TelegramAITaskTests(AITaskFixture, APITestCase):
    def setUp(self):
        super().setUp()
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=101, telegram_user_id=101, is_connected=True)
        self.bot = patch("apps.telegram_tasks.bot_api").start()
        self.message = {"message_id": 1, "chat": {"id": 101, "type": "private"}, "from": {"id": 101}, "text": self.payload["text"]}

    def webhook(self, secret="secret"):
        return self.client.post("/api/v1/telegram/webhook/", {"message": self.message}, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret)

    def callback(self, data, sender=101):
        callback = {"id": "cb", "from": {"id": sender}, "message": self.message, "data": data}
        return self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback},
                                format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret")

    def test_bot_asks_which_first_name_and_accepts_surname_reply(self):
        other = User.objects.create_user(username="second", email="second@example.com",
                                         first_name="Muslima", last_name="Karimova", department=self.department)
        self.parsed["assignee"] = "Muslima"
        self.assertIn("Which Muslima?", self.webhook().data["text"])
        self.assertFalse(Task.objects.exists())
        self.message["message_id"] = 2
        self.message["text"] = "Karimova"
        self.assertIn("✅", self.webhook().data["text"])
        self.assertEqual(Task.objects.get().main_assignee, other)
        self.extractor.assert_called_once()

    def test_reply_is_returned_in_webhook_response_without_extra_api_call(self):
        response = self.webhook()
        self.assertEqual(response.data["method"], "sendMessage")
        self.assertEqual(response.data["chat_id"], 101)
        self.bot.assert_not_called()
        self.assertEqual(Task.objects.count(), 1)

    def test_telegram_text_and_replayed_update_create_one_task(self):
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(Task.objects.count(), 1)
        self.extractor.assert_called_once()

    def test_start_shows_short_menu_with_two_buttons(self):
        self.message["text"] = "/start"
        response = self.webhook()
        self.assertIn("who · what · when", response.data["text"])
        buttons = [button["text"] for row in response.data["reply_markup"]["inline_keyboard"] for button in row]
        self.assertEqual(buttons, ["📅 Today", "💡 Example"])
        self.bot.assert_not_called()
        self.extractor.assert_not_called()

    def test_old_buttons_and_commands_still_open_the_menu(self):
        for data in ("task:create", "task:voice", "task:template", "task:menu"):
            with self.subTest(data=data):
                response = self.callback(data)
                self.assertEqual(response.data["method"], "sendMessage")
                self.assertIn("Tiko", response.data["text"])
        for command in ("/menu", "/create", "/template", "/voice"):
            with self.subTest(command=command):
                self.message["text"] = command
                self.assertIn("Tiko", self.webhook().data["text"])
        self.extractor.assert_not_called()
        self.assertFalse(Task.objects.exists())

    def test_example_and_help_screens(self):
        self.assertIn("Examples", self.callback("task:example").data["text"])
        self.message["text"] = "/help"
        self.assertIn("Everyone", self.webhook().data["text"])
        self.extractor.assert_not_called()

    def test_inline_callback_rejects_unlinked_sender(self):
        response = self.callback("task:example", sender=999)
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

    def test_expired_callback_still_sends_selected_screen(self):
        self.bot.side_effect = TelegramError("query is too old")
        response = self.callback("task:example")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Examples", response.data["text"])

    def test_task_confirmation_is_short_and_escapes_html(self):
        self.parsed["title"] = "Fix <login> & signup"
        response = self.webhook()
        self.assertTrue(response.data["text"].startswith("✅ <b>Fix &lt;login&gt; &amp; signup</b>"))
        self.assertIn("👤 Muslima Zokirjonova", response.data["text"])
        self.assertNotIn("Medium", response.data["text"])
        self.assertEqual(response.data["reply_markup"]["inline_keyboard"],
                         [[{"text": "Open", "url": task_url(str(Task.objects.get().pk))}]])

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

    @patch("apps.telegram_tasks.download_voice", side_effect=TelegramError("down"))
    def test_voice_download_failure_lets_telegram_retry(self, download):
        self.message.pop("text")
        self.message["voice"] = {"file_id": "abc", "file_unique_id": "unique"}
        self.assertEqual(self.webhook().status_code, 502)
        self.assertFalse(Task.objects.exists())

    @patch("apps.ai_tasks.extract_task_change")
    def test_telegram_delete_button_confirms_only_own_last_task(self, change_extractor):
        self.webhook()
        task_id = Task.objects.get().pk
        change_extractor.return_value = {
            "action": "delete", "target": "last", "title": None, "description": None,
            "assignee": None, "project": None, "due_date": None, "priority": None,
            "status": None, "clarification": None,
        }
        self.message["message_id"] = 2
        self.message["text"] = "Delete my last created task"
        asked = self.webhook()
        self.assertIn("Delete <b>", asked.data["text"])
        callback_data = asked.data["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(Task.objects.filter(pk=task_id).exists())
        response = self.callback(callback_data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Deleted", response.data["text"])
        self.assertFalse(Task.objects.filter(pk=task_id).exists())

    def test_assign_all_send_button_creates_tasks(self):
        self.message["text"] = "Hammaga: statuslarni yangilang, 11-noyabr"
        self.parsed["assignee"] = "all"
        asked = self.webhook()
        self.assertIn("to 1 employees (IT)?", asked.data["text"])
        send = asked.data["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(send["text"], "✅ Send")
        response = self.callback(send["callback_data"])
        self.assertIn("👥 1 employees · IT", response.data["text"])
        self.assertEqual(Task.objects.get().main_assignee, self.assignee)
        self.assertEqual(self.callback("task:cancel").data["text"], "Cancelled")

    @patch("apps.telegram_tasks.download_voice")
    @patch("apps.ai_tasks.transcribe", return_value="Muslima uchun vazifa")
    def test_voice_clarification_shows_heard_words(self, transcriber, download):
        download.return_value = SimpleUploadedFile("voice.ogg", b"audio")
        self.parsed["assignee"] = "Unknown Person"
        self.message.pop("text")
        self.message["voice"] = {"file_id": "abc", "file_unique_id": "unique"}
        self.assertIn("I heard: Muslima uchun vazifa", self.webhook().data["text"])


@override_settings(OPENAI_API_KEY="test", OPENAI_TASK_MODEL="gpt-5.4-mini", OPENAI_TRANSCRIPTION_MODEL="gpt-4o-mini-transcribe")
class AIProviderTests(SimpleTestCase):
    @patch("apps.ai_provider.request_ai")
    def test_refusal_or_malformed_output_is_not_a_task(self, provider):
        for response in [{}, {"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}, {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}]:
            provider.return_value = response
            with self.assertRaises(AIUnavailable):
                extract_task("create a task")

    @patch("apps.ai_provider.request_ai", return_value={})
    def test_task_extraction_uses_low_reasoning_only_for_reasoning_models(self, provider):
        with self.assertRaises(AIUnavailable):
            extract_task("create a task")
        self.assertEqual(json.loads(provider.call_args.args[1])["reasoning_effort"], "low")
        with override_settings(OPENAI_TASK_MODEL="gpt-4.1-mini"), self.assertRaises(AIUnavailable):
            extract_task("create a task")
        self.assertNotIn("reasoning_effort", json.loads(provider.call_args.args[1]))

    @patch("apps.ai_provider.request_ai", return_value={"text": "Muslima uchun task"})
    def test_audio_is_uploaded_with_safe_filename(self, provider):
        self.assertEqual(transcribe(SimpleUploadedFile("voice.ogg", b"sample audio")), "Muslima uchun task")
        args = provider.call_args.args
        self.assertEqual(args[0], "audio/transcriptions")
        self.assertIn(b'filename="voice.ogg"', args[1])
        self.assertIn(b'name="prompt"', args[1])
        self.assertIn(b"Keep employee names as said", args[1])
        self.assertIn("завтра".encode(), args[1])
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
