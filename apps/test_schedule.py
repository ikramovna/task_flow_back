import json
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .ai_provider import AIUnavailable, extract_event, extract_schedule
from .ai_tasks import task_intent
from .models import Department, Event, Notification, Task, TelegramIntegration, User


def day(offset):
    return (timezone.localdate() + timedelta(days=offset)).isoformat()


def event(title, offset, time=None, people=(), end=None, location=None):
    return {"title": title, "date": day(offset), "time": time, "end_time": end, "location": location,
            "attendees": list(people)}


class EventFixture:
    def setUp(self):
        self.department = Department.objects.create(name="IT", code="it")
        self.user = User.objects.create_user(username="boss", email="boss@example.com", role="manager",
                                             first_name="Muslima", last_name="Zokirjonova", department=self.department)
        make = lambda first, last: User.objects.create_user(  # noqa: E731
            username=f"{first}{last}".lower(), email=f"{first}.{last}@example.com".lower(),
            first_name=first, last_name=last, department=self.department)
        self.karimova = make("Jasmin", "Karimova")
        self.rakhimova = make("Jasmin", "Rakhimova")
        self.sardor = make("Sardor", "Aliyev")
        self.nodira = make("Nodira", "Saidova")
        TelegramIntegration.objects.create(user=self.user, telegram_chat_id=5, telegram_user_id=5, is_connected=True)
        self.bot = patch("apps.telegram_tasks.bot_api").start()
        self.addCleanup(patch.stopall)
        self.message = {"message_id": 1, "chat": {"id": 5, "type": "private"}, "from": {"id": 5}}

    def send(self, **message):
        return self.client.post("/api/v1/telegram/webhook/", {"message": {**self.message, **message}}, format="json",
                                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret").data

    def press(self, data):
        callback = {"id": "cb", "from": {"id": 5}, "message": self.message, "data": data}
        return self.client.post("/api/v1/telegram/webhook/", {"callback_query": callback}, format="json",
                                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret").data

    def buttons(self, reply):
        return [button for row in reply["reply_markup"]["inline_keyboard"] for button in row]

    def button(self, reply, starts):
        return next(b for b in self.buttons(reply) if b["text"].startswith(starts))


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret")
class CalendarPhotoTests(EventFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.extracted = {"clarification": None, "events": [
            event("MEPT Tashkent", -6, "09:00", ["Sardor"]),
            event("Open Doors", 3),
            event("MEPT Samarkand", 5, "14:00", ["Jasmin", "Nodira"], location="Samarkand"),
            event("Live session", 6, "15:00", ["Jasmin Rakhimova", "Sardor"], end="16:30"),
            event("Karshi State University visit", 7, "10:00", ["Jasmin", "Ghost"]),
        ]}
        self.reader = patch("apps.ai_schedule.extract_schedule", return_value=self.extracted).start()
        self.download = patch("apps.telegram_tasks.download_file", return_value=b"jpeg").start()
        self.message["photo"] = [{"file_id": "small", "file_size": 10},
                                 {"file_id": "big", "file_unique_id": "u", "file_size": 900}]

    def test_calendar_photo_adds_events_not_tasks(self):
        asked = self.send()
        self.download.assert_called_once_with("big", 10 * 1024 * 1024, "image")
        self.assertIn("Which <b>Jasmin</b>?", asked["text"])
        self.assertEqual([b["text"] for b in self.buttons(asked)], ["Jasmin Karimova", "Jasmin Rakhimova", "Skip"])

        preview = self.press(self.buttons(asked)[0]["callback_data"])
        self.assertIn("4 events", preview["text"])  # the past one is skipped, the unnamed one is kept
        self.assertIn("1 past events skipped", preview["text"])
        self.assertIn("❔ Ghost", preview["text"])
        add = self.button(preview, "✅ Add")
        self.assertEqual(add["text"], "✅ Add 4 to calendar")

        with self.captureOnCommitCallbacks(execute=True):
            done = self.press(add["callback_data"])
        self.assertIn("Added 4 events to calendar", done["text"])
        self.assertIn("5 people notified", done["text"])
        self.assertFalse(Task.objects.exists())
        self.assertEqual(Event.objects.count(), 4)

        samarkand = Event.objects.get(title="MEPT Samarkand")
        self.assertEqual(set(samarkand.attendees.all()), {self.karimova, self.nodira})
        self.assertEqual(timezone.localtime(samarkand.starts_at).strftime("%H:%M"), "14:00")
        self.assertEqual(samarkand.ends_at - samarkand.starts_at, timedelta(hours=1))
        self.assertEqual(samarkand.location, "Samarkand")
        live = Event.objects.get(title="Live session")
        self.assertEqual(timezone.localtime(live.ends_at).strftime("%H:%M"), "16:30")
        self.assertEqual(set(live.attendees.all()), {self.rakhimova, self.sardor})
        doors = Event.objects.get(title="Open Doors")
        self.assertFalse(doors.attendees.exists())
        self.assertEqual(timezone.localtime(doors.starts_at).strftime("%H:%M"), "09:00")
        self.assertEqual(Notification.objects.filter(notification_type="event_invite").count(), 5)
        self.assertEqual(Event.objects.get(title="Karshi State University visit").created_by, self.user)
        # The list cannot be added twice.
        self.assertIn("expired", self.press(add["callback_data"])["text"])
        self.assertEqual(Event.objects.count(), 4)

    def test_skip_leaves_that_name_out_everywhere(self):
        asked = self.send()
        preview = self.press(self.buttons(asked)[-1]["callback_data"])
        self.assertIn("4 events", preview["text"])
        self.press(self.button(preview, "✅ Add")["callback_data"])
        self.assertEqual(set(Event.objects.get(title="MEPT Samarkand").attendees.all()), {self.nodira})

    def test_people_are_listed_as_buttons_and_can_be_removed_from_every_event(self):
        asked = self.send()
        preview = self.press(self.buttons(asked)[0]["callback_data"])
        people = [b["text"] for b in self.buttons(preview) if b["text"][0] in "✅⬜" and "Add" not in b["text"]]
        self.assertEqual(people, ["✅ Jasmin Karimova", "✅ Nodira Saidova", "✅ Jasmin Rakhimova", "✅ Sardor Aliyev"])
        # Untick Jasmin Karimova: she leaves both of her events.
        preview = self.press(self.button(preview, "✅ Jasmin Karimova")["callback_data"])
        self.assertTrue(self.button(preview, "⬜ Jasmin Karimova"))
        self.assertNotIn("Jasmin Karimova,", preview["text"])
        # Tick and untick again: the choice can be changed until Add.
        preview = self.press(self.button(preview, "⬜ Jasmin Karimova")["callback_data"])
        preview = self.press(self.button(preview, "✅ Jasmin Karimova")["callback_data"])
        with self.captureOnCommitCallbacks(execute=True):
            done = self.press(self.button(preview, "✅ Add")["callback_data"])
        self.assertIn("3 people notified", done["text"])
        self.assertEqual(set(Event.objects.get(title="MEPT Samarkand").attendees.all()), {self.nodira})
        self.assertFalse(Event.objects.get(title="Karshi State University visit").attendees.exists())
        self.assertFalse(Notification.objects.filter(recipient=self.karimova).exists())

    def test_cancel_adds_nothing(self):
        asked = self.send()
        preview = self.press(self.buttons(asked)[0]["callback_data"])
        self.assertEqual(self.press(self.button(preview, "✖️ Cancel")["callback_data"])["text"], "✖️ Cancelled")
        self.assertIn("expired", self.press(self.button(preview, "✅ Add")["callback_data"])["text"])
        self.assertFalse(Event.objects.exists())

    def test_replayed_photo_update_reads_the_image_once(self):
        self.assertEqual(self.send()["text"], self.send()["text"])
        self.reader.assert_called_once()

    def test_image_without_upcoming_events_explains(self):
        self.extracted["events"] = [event("Old", -2)]
        self.assertIn("no upcoming events", self.send()["text"])

    def test_image_document_is_accepted_but_other_files_are_not(self):
        self.message.pop("photo")
        self.assertIn("Which", self.send(document={"file_id": "doc", "mime_type": "image/png"})["text"])
        self.message["message_id"] = 2
        self.assertIn("Tiko", self.send(document={"file_id": "pdf", "mime_type": "application/pdf"})["text"])


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret")
class MeetingRequestTests(EventFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.parsed = {"clarification": None, "events": [
            event("Meeting: monthly report", 2, "14:00", ["Sardor", "Nodira"], end="15:30")]}
        self.reader = patch("apps.ai_schedule.extract_event", return_value=self.parsed).start()
        self.task_reader = patch("apps.ai_tasks.extract_task").start()

    def test_text_meeting_previews_then_adds_to_calendar(self):
        preview = self.send(text="Indinga soat 14:00 da Sardor va Nodira bilan oylik hisobot bo'yicha yig'ilish")
        self.assertIn("📅 <b>Meeting: monthly report</b>", preview["text"])
        self.assertIn("14:00–15:30", preview["text"])
        self.assertIn("Sardor Aliyev, Nodira Saidova", preview["text"])
        self.assertFalse(Event.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            done = self.press(self.button(preview, "✅ Add")["callback_data"])
        self.assertIn("✅ <b>Meeting: monthly report</b>", done["text"])
        self.assertIn("2 people notified", done["text"])
        meeting = Event.objects.get()
        self.assertEqual(set(meeting.attendees.all()), {self.sardor, self.nodira})
        self.assertEqual(meeting.event_type, "meeting")
        self.assertEqual(meeting.department, self.department)
        self.task_reader.assert_not_called()
        notification = Notification.objects.get(recipient=self.sardor)
        self.assertEqual(notification.title, "New meeting: Meeting: monthly report")
        self.assertIn("from Muslima Zokirjonova", notification.body)

    def test_all_staff_meeting_invites_the_team(self):
        self.parsed["events"][0]["attendees"] = ["all"]
        preview = self.send(text="Ertaga soat 10 da hammaga yig'ilish")
        self.assertIn("Jasmin Karimova, Jasmin Rakhimova, Nodira Saidova, Sardor Aliyev", preview["text"])
        self.assertNotIn("Muslima", preview["text"])  # the organiser is not invited to their own meeting
        self.press(self.button(preview, "✅ Add")["callback_data"])
        self.assertEqual(Event.objects.get().attendees.count(), 4)

    def test_all_staff_is_ignored_for_members(self):
        self.user.role = "member"
        self.user.save(update_fields=["role"])
        self.parsed["events"][0]["attendees"] = ["all"]
        self.assertIn("❔ all", self.send(text="Ertaga hammaga yig'ilish soat 10")["text"])

    def test_single_meeting_attendees_can_be_unticked(self):
        preview = self.send(text="Indinga soat 14:00 da Sardor va Nodira bilan yig'ilish")
        preview = self.press(self.button(preview, "✅ Sardor Aliyev")["callback_data"])
        self.assertIn("👥 Nodira Saidova", preview["text"])
        self.press(self.button(preview, "✅ Add")["callback_data"])
        self.assertEqual(list(Event.objects.get().attendees.all()), [self.nodira])

    def test_ambiguous_attendee_is_asked(self):
        self.parsed["events"][0]["attendees"] = ["Jasmin"]
        asked = self.send(text="Meeting with Jasmin tomorrow 10:00")
        self.assertIn("Which <b>Jasmin</b>?", asked["text"])
        preview = self.press(self.buttons(asked)[1]["callback_data"])
        self.assertIn("Jasmin Rakhimova", preview["text"])

    def test_missing_time_is_asked(self):
        self.parsed["events"][0]["time"] = None
        self.assertIn("What time", self.send(text="Meeting with Sardor on Friday")["text"])

    def test_web_can_confirm_with_text(self):
        self.client.force_authenticate(self.user)
        asked = self.client.post("/api/v1/ai/tasks/", {"request_id": "6f1c2a7e-8d2b-4c55-9c39-111111111111",
                                                        "text": "Meeting with Sardor tomorrow 14:00"}, format="json").data
        self.assertIn("CONFIRM EVENT", asked["message"])
        done = self.client.post("/api/v1/ai/tasks/", {"request_id": "6f1c2a7e-8d2b-4c55-9c39-222222222222",
                                                       "text": f"CONFIRM EVENT {asked['confirmation_code']}"},
                                format="json").data
        self.assertEqual(done["status"], "created")
        self.assertTrue(Event.objects.exists())


class MeetingIntentTests(SimpleTestCase):
    def test_meeting_words_go_to_the_calendar(self):
        for text in ("Meeting on Friday at 14:00 with Ali", "Ertaga hammaga yig'ilish", "Uchrashuv Muslima bilan",
                     "Совещание в пятницу в 10", "Встреча со всеми сотрудниками завтра"):
            with self.subTest(text=text):
                self.assertEqual(task_intent(text), "event")
        self.assertEqual(task_intent("Create a task: prepare slides for the meeting"), "create")
        self.assertEqual(task_intent("Ali, fix the login page, Friday"), "create")


@override_settings(OPENAI_API_KEY="test", OPENAI_TASK_MODEL="gpt-5.4-mini")
class EventProviderTests(SimpleTestCase):
    def answer(self, content):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}

    @patch("apps.ai_provider.request_ai")
    def test_photo_is_sent_and_output_cleaned(self, provider):
        provider.return_value = self.answer({"clarification": None, "events": [
            {"title": " MY FAIR Tashkent ", "date": "2026-11-11", "time": "10:00", "end_time": "20:00",
             "location": "AZIMUT Grand Hotel", "attendees": ["Abdunosir", " ", "Abdunosir"]}]})
        result = extract_schedule(b"img", "image/jpeg", "only fairs")
        self.assertEqual(result["events"], [{"title": "MY FAIR Tashkent", "date": "2026-11-11", "time": "10:00",
                                             "end_time": "20:00", "location": "AZIMUT Grand Hotel",
                                             "attendees": ["Abdunosir"]}])
        content = json.loads(provider.call_args.args[1])["messages"][1]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("only fairs", content[0]["text"])
        self.assertEqual(provider.call_args.kwargs["timeout"], 90)

    @patch("apps.ai_provider.request_ai")
    def test_meeting_text_uses_the_same_shape(self, provider):
        provider.return_value = self.answer({"clarification": None, "events": [
            {"title": "Meeting: report", "date": "2026-11-12", "time": "14:00", "end_time": None,
             "location": None, "attendees": ["all"]}]})
        self.assertEqual(extract_event("hammaga yig'ilish")["events"][0]["attendees"], ["all"])

    @patch("apps.ai_provider.request_ai")
    def test_malformed_output_is_rejected(self, provider):
        provider.return_value = self.answer({"clarification": None, "events": [{"title": 1}]})
        with self.assertRaises(AIUnavailable):
            extract_schedule(b"img", "image/jpeg")
