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

    def test_time_entry_endpoint_is_removed(self):
        response = self.client.get("/api/v1/time-entries/")
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

    def test_privileged_roles_can_assign_task_across_departments(self):
        other_department = Department.objects.create(name="Operations", code="operations-tasks")
        outsider = User.objects.create_user(
            username="task-outsider",
            email="task-outsider@example.com",
            password="pass12345",
            department=other_department,
        )
        users_by_role = {User.Role.OWNER: self.owner}
        for role in (User.Role.ADMIN, User.Role.MANAGER):
            users_by_role[role] = User.objects.create_user(
                username=f"task-{role}",
                email=f"task-{role}@example.com",
                password="pass12345",
                department=self.department,
                role=role,
            )

        for role, requester in users_by_role.items():
            with self.subTest(role=role):
                self.client.force_authenticate(requester)
                response = self.client.post(
                    "/api/v1/tasks/",
                    {
                        "department": self.department.pk,
                        "title": f"{role} cross-department task",
                        "assignees": [outsider.pk],
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertTrue(
                    Task.objects.get(title=f"{role} cross-department task")
                    .assignees.filter(pk=outsider.pk)
                    .exists()
                )

    def test_manager_has_admin_access_across_departments(self):
        other_department = Department.objects.create(name="Finance", code="finance")
        manager = User.objects.create_user(
            username="manager",
            email="manager@example.com",
            password="pass12345",
            department=self.department,
            role=User.Role.MANAGER,
        )
        self.client.force_authenticate(manager)

        response = self.client.post(
            "/api/v1/tasks/",
            {"department": other_department.pk, "title": "Prepare budget"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(response.data["department"]), str(other_department.pk))
        self.assertEqual(self.client.get("/api/v1/departments/").data["count"], 2)

    def test_manager_sees_only_assigned_or_self_created_tasks(self):
        manager = User.objects.create_user(
            username="manager-tasks",
            email="manager-tasks@example.com",
            password="pass12345",
            department=self.department,
            role=User.Role.MANAGER,
        )
        assigned = Task.objects.create(
            department=self.department,
            title="Assigned to manager",
            created_by=self.owner,
        )
        assigned.assignees.add(manager)
        Task.objects.create(
            department=self.department,
            title="Created by manager",
            created_by=manager,
        )
        Task.objects.create(
            department=self.department,
            title="Unrelated task",
            created_by=self.owner,
        )
        self.client.force_authenticate(manager)

        response = self.client.get("/api/v1/tasks/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {item["title"] for item in response.data["results"]},
            {"Assigned to manager", "Created by manager"},
        )

    def test_member_cannot_archive_assigned_completed_task(self):
        task = Task.objects.create(
            department=self.department,
            title="Completed member task",
            created_by=self.owner,
            status=Task.Status.COMPLETED,
        )
        task.assignees.add(self.member)
        self.client.force_authenticate(self.member)

        response = self.client.post(f"/api/v1/tasks/{task.pk}/archive/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        task.refresh_from_db()
        self.assertFalse(task.is_archived)

    def test_manager_can_archive_completed_task_and_list_archive(self):
        manager = User.objects.create_user(
            username="archive-manager",
            email="archive-manager@example.com",
            password="pass12345",
            department=self.department,
            role=User.Role.MANAGER,
        )
        task = Task.objects.create(
            department=self.department,
            title="Manager completed task",
            created_by=manager,
            status=Task.Status.COMPLETED,
        )
        self.client.force_authenticate(manager)

        response = self.client.post(f"/api/v1/tasks/{task.pk}/archive/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_archived"])
        self.assertEqual(self.client.get("/api/v1/tasks/").data["count"], 0)
        archived = self.client.get("/api/v1/tasks/", {"archived": "true"})
        self.assertEqual(archived.data["count"], 1)

    def test_task_accepts_backlog_and_on_hold_statuses(self):
        for task_status in (Task.Status.BACKLOG, Task.Status.ON_HOLD):
            response = self.client.post(
                "/api/v1/tasks/",
                {
                    "department": self.department.pk,
                    "title": f"Task {task_status}",
                    "status": task_status,
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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

    def test_dashboard_returns_english_sections(self):
        Task.objects.create(
            department=self.department,
            title="Prepare student list",
            created_by=self.owner,
            status=Task.Status.IN_PROGRESS,
            due_date=timezone.localdate() + timedelta(days=2),
        )
        Task.objects.create(
            department=self.department,
            title="Future task",
            created_by=self.owner,
            status=Task.Status.BACKLOG,
        )
        Task.objects.create(
            department=self.department,
            title="Paused task",
            created_by=self.owner,
            status=Task.Status.ON_HOLD,
        )
        starts_at = timezone.now() + timedelta(hours=1)
        Event.objects.create(
            department=self.department,
            title="Department meeting",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            created_by=self.owner,
        )

        response = self.client.get("/api/v1/dashboard/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("summary", response.data)
        self.assertIn("today_events", response.data)
        self.assertIn("upcoming_events", response.data)
        self.assertIn("upcoming_deadlines", response.data)
        self.assertIn("tasks_by_department", response.data)
        self.assertIn("recent_tasks", response.data)
        self.assertEqual(response.data["summary"]["in_progress_tasks"]["count"], 1)
        self.assertEqual(response.data["summary"]["backlog_tasks"]["count"], 1)
        self.assertEqual(response.data["summary"]["on_hold_tasks"]["count"], 1)

    def test_privileged_roles_can_invite_attendee_from_another_department(self):
        other_department = Department.objects.create(name="Finance", code="finance-events")
        outsider = User.objects.create_user(
            username="outsider",
            email="outsider@example.com",
            password="pass12345",
            department=other_department,
        )
        starts_at = timezone.now()
        users_by_role = {User.Role.OWNER: self.owner}
        for role in (User.Role.ADMIN, User.Role.MANAGER):
            users_by_role[role] = User.objects.create_user(
                username=f"event-{role}",
                email=f"event-{role}@example.com",
                password="pass12345",
                department=self.department,
                role=role,
            )

        for role, requester in users_by_role.items():
            with self.subTest(role=role):
                self.client.force_authenticate(requester)
                response = self.client.post(
                    "/api/v1/events/",
                    {
                        "department": self.department.pk,
                        "title": f"{role} planning",
                        "starts_at": starts_at,
                        "ends_at": starts_at + timedelta(hours=1),
                        "attendees": [outsider.pk],
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertTrue(
                    Event.objects.get(title=f"{role} planning")
                    .attendees.filter(pk=outsider.pk)
                    .exists()
                )

    def test_member_cannot_invite_attendee_from_another_department(self):
        other_department = Department.objects.create(name="Legal", code="legal-events")
        outsider = User.objects.create_user(
            username="member-outsider",
            email="member-outsider@example.com",
            password="pass12345",
            department=other_department,
        )
        starts_at = timezone.now()
        self.client.force_authenticate(self.member)

        response = self.client.post(
            "/api/v1/events/",
            {
                "department": self.department.pk,
                "title": "Member planning",
                "starts_at": starts_at,
                "ends_at": starts_at + timedelta(hours=1),
                "attendees": [outsider.pk],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Event.objects.filter(title="Member planning").exists())
