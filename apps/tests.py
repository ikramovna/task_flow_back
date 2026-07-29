from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Conversation, ConversationParticipant, Membership, Project, Report, Task, User, Workspace


class TaskFlowAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sarah", email="sarah@example.com", password="StrongPass123")
        self.other = User.objects.create_user(username="mike", email="mike@example.com", password="StrongPass123")
        self.workspace = Workspace.objects.create(name="Acme", slug="acme", owner=self.user)
        Membership.objects.create(workspace=self.workspace, user=self.user, role=Membership.Role.OWNER)
        Membership.objects.create(workspace=self.workspace, user=self.other)
        self.project = Project.objects.create(workspace=self.workspace, name="Website", created_by=self.user, status=Project.Status.IN_PROGRESS)
        self.client.force_authenticate(self.user)

    def test_workspace_isolation(self):
        stranger = User.objects.create_user(username="stranger", email="stranger@example.com", password="StrongPass123")
        hidden = Workspace.objects.create(name="Hidden", slug="hidden", owner=stranger)
        response = self.client.get("/api/v1/workspaces/")
        ids = [row["id"] for row in response.data]
        self.assertIn(str(self.workspace.id), ids)
        self.assertNotIn(str(hidden.id), ids)

    def test_create_and_complete_task(self):
        payload = {"project": str(self.project.id), "title": "Build API", "priority": "high", "assignees": [self.other.id], "progress": 50}
        created = self.client.post("/api/v1/tasks/", payload, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        updated = self.client.patch(f"/api/v1/tasks/{created.data['id']}/", {"status": "completed"}, format="json")
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        task = Task.objects.get(id=created.data["id"])
        self.assertEqual(task.progress, 100)
        self.assertIsNotNone(task.completed_at)

    def test_calendar_rejects_invalid_time_range(self):
        now = timezone.now()
        payload = {"workspace": str(self.workspace.id), "title": "Review", "starts_at": now.isoformat(), "ends_at": (now - timedelta(hours=1)).isoformat()}
        response = self.client.post("/api/v1/events/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_analytics_requires_workspace(self):
        response = self.client.get("/api/v1/analytics/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_profile_preferences_and_password(self):
        profile = self.client.patch("/api/v1/me/", {"first_name": "Sarah", "job_title": "Manager"}, format="json")
        self.assertEqual(profile.status_code, status.HTTP_200_OK)
        preferences = self.client.patch("/api/v1/me/preferences/", {"theme": "dark", "weekly_reports": True}, format="json")
        self.assertEqual(preferences.status_code, status.HTTP_200_OK)
        changed = self.client.post("/api/v1/me/change-password/", {"current_password": "StrongPass123", "new_password": "NewStrongPass456", "confirm_password": "NewStrongPass456"}, format="json")
        self.assertEqual(changed.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewStrongPass456"))

    def test_messages_are_visible_only_to_participants(self):
        conversation = Conversation.objects.create(workspace=self.workspace)
        ConversationParticipant.objects.create(conversation=conversation, user=self.user)
        response = self.client.post("/api/v1/messages/", {"conversation": str(conversation.id), "body": "Hello"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.client.force_authenticate(self.other)
        hidden = self.client.get("/api/v1/messages/")
        self.assertEqual(hidden.data["count"], 0)

    def test_report_is_generated_as_csv(self):
        response = self.client.post("/api/v1/reports/", {"workspace": str(self.workspace.id), "name": "Weekly", "report_type": "weekly_progress"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        report = Report.objects.get(pk=response.data["id"])
        self.assertEqual(report.status, Report.Status.READY)
        self.assertTrue(report.file.name.endswith(".csv"))
        report.file.delete(save=False)
