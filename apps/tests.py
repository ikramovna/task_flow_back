from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Department, Event, Task, User


class DepartmentScopedApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner", email="owner@example.com", password="pass12345"
        )
        self.member = User.objects.create_user(
            username="member", email="member@example.com", password="pass12345"
        )
        self.department = Department.objects.create(name="Engineering", code="engineering")
        self.owner.department = self.department
        self.owner.role = User.Role.OWNER
        self.owner.save(update_fields=["department", "role"])
        self.member.department = self.department
        self.member.save(update_fields=["department"])
        self.client.force_authenticate(self.owner)

    def test_workspace_endpoint_is_removed(self):
        response = self.client.get("/api/v1/workspaces/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_faq_endpoint_is_removed(self):
        response = self.client.get("/api/v1/faqs/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_support_ticket_endpoint_is_removed(self):
        response = self.client.get("/api/v1/support-tickets/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_departments_are_top_level_resources(self):
        response = self.client.get("/api/v1/departments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data["results"][0]
        self.assertEqual(item["code"], "engineering")
        self.assertNotIn("workspace", item)

    def test_project_endpoint_is_removed(self):
        response = self.client.get("/api/v1/projects/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_task_uses_department_directly(self):
        response = self.client.post(
            "/api/v1/tasks/",
            {"department": self.department.pk, "title": "Update website"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("project", response.data)

    def test_member_can_only_be_created_for_department(self):
        response = self.client.post(
            "/api/v1/members/",
            {
                "department": self.department.pk,
                "username": "new",
                "email": "new@example.com",
                "role": User.Role.MEMBER,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("workspace", response.data)

    def test_analytics_is_scoped_by_department(self):
        Task.objects.create(
            department=self.department,
            title="Done",
            created_by=self.owner,
            status=Task.Status.COMPLETED,
            completed_at=timezone.now(),
        )
        response = self.client.get(
            "/api/v1/analytics/", {"department": self.department.pk}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["task_completion_rate"], 100)

    def test_event_attendees_must_belong_to_department(self):
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pass12345"
        )
        starts_at = timezone.now()
        response = self.client.post(
            "/api/v1/events/",
            {
                "department": self.department.pk,
                "title": "Planning",
                "starts_at": starts_at,
                "ends_at": starts_at + timedelta(hours=1),
                "attendees": [outsider.pk],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Event.objects.filter(title="Planning").exists())
