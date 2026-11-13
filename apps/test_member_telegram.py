from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import Department, TelegramIntegration, User


class MemberTelegramStatusTests(APITestCase):
    def setUp(self):
        self.department = Department.objects.create(name="IT", code="it")
        self.manager = User.objects.create_user(username="boss", email="boss@example.com", role="manager",
                                                department=self.department)
        self.member = User.objects.create_user(username="ali", email="ali@example.com", department=self.department)
        self.other = User.objects.create_user(username="sara", email="sara@example.com", department=self.department)
        TelegramIntegration.objects.create(user=self.member, telegram_user_id=1, telegram_chat_id=1,
                                           telegram_username="ali_tg", is_connected=True, connected_at=timezone.now())
        # Started connecting but never finished: not connected.
        TelegramIntegration.objects.create(user=self.other, link_token="t", is_connected=False)

    def statuses(self, viewer):
        self.client.force_authenticate(viewer)
        rows = self.client.get("/api/v1/members/").data["results"]
        return {row["email"]: row["telegram"] for row in rows}

    def test_staff_list_shows_who_connected_telegram(self):
        statuses = self.statuses(self.manager)
        self.assertTrue(statuses["ali@example.com"]["connected"])
        self.assertEqual(statuses["ali@example.com"]["username"], "ali_tg")
        self.assertIsNotNone(statuses["ali@example.com"]["connected_at"])
        self.assertEqual(statuses["sara@example.com"], {"connected": False, "username": "", "connected_at": None})
        self.assertEqual(statuses["boss@example.com"], {"connected": False, "username": "", "connected_at": None})

    def test_members_see_the_status_but_not_other_peoples_username(self):
        statuses = self.statuses(self.other)
        self.assertTrue(statuses["ali@example.com"]["connected"])
        self.assertEqual(statuses["ali@example.com"]["username"], "")
        self.assertEqual(self.statuses(self.member)["ali@example.com"]["username"], "ali_tg")

    def test_status_does_not_add_a_query_per_person(self):
        self.client.force_authenticate(self.manager)
        with CaptureQueriesContext(connection) as few:
            self.client.get("/api/v1/members/")
        for index in range(5):
            person = User.objects.create_user(username=f"u{index}", email=f"u{index}@example.com",
                                              department=self.department)
            TelegramIntegration.objects.create(user=person, telegram_user_id=10 + index,
                                               telegram_chat_id=10 + index, is_connected=True)
        with CaptureQueriesContext(connection) as many:
            self.client.get("/api/v1/members/")
        self.assertEqual(len(few), len(many))
