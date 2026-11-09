import json
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .ai_provider import AIUnavailable, extract_schedule
from .models import Department, Task, TelegramIntegration, User


def day(offset):
    return (timezone.localdate() + timedelta(days=offset)).isoformat()


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret")
class TelegramScheduleTests(APITestCase):
    def setUp(self):
        self.department = Department.objects.create(name="IT", code="it")
        self.user = User.objects.create_user(username="boss", email="boss@example.com", role="manager",
                                             department=self.department)
        make = lambda first, last: User.objects.create_user(  # noqa: E731
            username=f"{first}{last}".lower(), email=f"{first}.{last}@example.com".lower(),
            first_name=first, last_name=last, department=self.department)
        self.karimova = make("Jasmin", "Karimova")
        self.rakhimova = make("Jasmin", "Rakhimova")
        self.sardor = make("Sardor", "Aliyev")
        self.nodira = make("Nodira", "Saidova")
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=5, telegram_user_id=5, is_connected=True)
        self.extracted = {"clarification": None, "tasks": [
            {"title": "MEPT Tashkent", "date": day(-6), "time": "09:00", "assignees": ["Sardor"]},
            {"title": "Open Doors", "date": day(3), "time": None, "assignees": []},
            {"title": "MEPT Samarkand", "date": day(5), "time": "14:00", "assignees": ["Jasmin", "Nodira"]},
            {"title": "Live session", "date": day(6), "time": "15:00", "assignees": ["Jasmin Rakhimova", "Sardor"]},
            {"title": "Karshi State University visit", "date": day(7), "time": None, "assignees": ["Jasmin", "Ghost"]},
        ]}
        self.reader = patch("apps.ai_schedule.extract_schedule", return_value=self.extracted).start()
        self.download = patch("apps.telegram_tasks.download_file", return_value=b"jpeg").start()
        self.bot = patch("apps.telegram_tasks.bot_api").start()
        self.addCleanup(patch.stopall)
        self.message = {"message_id": 1, "chat": {"id": 5, "type": "private"}, "from": {"id": 5},
                        "photo": [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_unique_id": "u", "file_size": 900}]}

    def send_photo(self):
        return self.client.post("/api/v1/telegram/webhook/", {"message": self.message}, format="json",
                                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret").data

    def press(self, data):
        callback = {"id": "cb", "from": {"id": 5}, "message": self.message, "data": data}
        return self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback}, format="json",
                                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret").data

    def buttons(self, reply):
        return [button for row in reply["reply_markup"]["inline_keyboard"] for button in row]

    def test_photo_asks_ambiguous_name_once_then_creates_one_task_per_person(self):
        asked = self.send_photo()
        self.download.assert_called_once_with("big", 10 * 1024 * 1024, "image")
        self.assertIn("Which <b>Jasmin</b>?", asked["text"])
        self.assertEqual([b["text"] for b in self.buttons(asked)], ["Jasmin Karimova", "Jasmin Rakhimova", "Skip"])
        self.assertFalse(Task.objects.exists())

        preview = self.press(self.buttons(asked)[0]["callback_data"])
        # Both "Jasmin" events got Karimova; the unknown name and past/unnamed events are reported.
        self.assertIn("5 tasks", preview["text"])
        self.assertIn("❔ Ghost", preview["text"])
        self.assertIn("2 past or unnamed events skipped", preview["text"])
        create = self.buttons(preview)[0]
        self.assertEqual(create["text"], "✅ Create 5")

        done = self.press(create["callback_data"])
        self.assertIn("Created 5 tasks", done["text"])
        assigned = sorted((t.title, t.main_assignee.last_name) for t in Task.objects.all())
        self.assertEqual(assigned, [
            ("Karshi State University visit", "Karimova"),
            ("Live session", "Aliyev"), ("Live session", "Rakhimova"),
            ("MEPT Samarkand", "Karimova"), ("MEPT Samarkand", "Saidova"),
        ])
        self.assertEqual(Task.objects.get(title="MEPT Samarkand", main_assignee=self.nodira).description, "Time: 14:00")
        # The list cannot be created twice.
        self.assertIn("expired", self.press(create["callback_data"])["text"])
        self.assertEqual(Task.objects.count(), 5)

    def test_skip_leaves_that_name_out_everywhere(self):
        asked = self.send_photo()
        preview = self.press(self.buttons(asked)[-1]["callback_data"])
        self.assertIn("3 tasks", preview["text"])

    def test_replayed_photo_update_reads_the_image_once(self):
        first = self.send_photo()
        second = self.send_photo()
        self.assertEqual(first["text"], second["text"])
        self.reader.assert_called_once()

    def test_image_without_people_explains(self):
        self.extracted["tasks"] = [{"title": "Open Doors", "date": day(3), "time": None, "assignees": []}]
        self.assertIn("no upcoming events", self.send_photo()["text"])

    def test_image_document_is_accepted_but_other_files_are_not(self):
        self.message.pop("photo")
        self.message["document"] = {"file_id": "doc", "mime_type": "image/png"}
        self.assertIn("Which", self.send_photo()["text"])
        self.message["message_id"] = 2
        self.message["document"] = {"file_id": "pdf", "mime_type": "application/pdf"}
        self.assertIn("Tiko", self.send_photo()["text"])


@override_settings(OPENAI_API_KEY="test", OPENAI_TASK_MODEL="gpt-5.4-mini")
class ScheduleProviderTests(SimpleTestCase):
    def answer(self, content):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}

    @patch("apps.ai_provider.request_ai")
    def test_sends_image_and_cleans_output(self, provider):
        provider.return_value = self.answer({"clarification": None, "tasks": [
            {"title": " MY FAIR Tashkent ", "date": "2026-11-11", "time": "10:00", "assignees": ["Abdunosir", " "]}]})
        result = extract_schedule(b"img", "image/jpeg", "only fairs")
        self.assertEqual(result["tasks"], [{"title": "MY FAIR Tashkent", "date": "2026-11-11", "time": "10:00",
                                            "assignees": ["Abdunosir"]}])
        payload = json.loads(provider.call_args.args[1])
        content = payload["messages"][1]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("only fairs", content[0]["text"])
        self.assertEqual(provider.call_args.kwargs["timeout"], 90)

    @patch("apps.ai_provider.request_ai")
    def test_malformed_output_is_rejected(self, provider):
        provider.return_value = self.answer({"clarification": None, "tasks": [{"title": 1}]})
        with self.assertRaises(AIUnavailable):
            extract_schedule(b"img", "image/jpeg")
