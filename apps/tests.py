from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Conversation, ConversationParticipant, Department, Event, Message, Notification, Task, TelegramIntegration, User, UserPreference
from .notifications import generate_deadline_notifications


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="reset-user",
            email="reset@example.com",
            password="OldPassword123!",
        )

    def test_request_sends_reset_email_without_authentication(self):
        response = self.client.post(
            "/api/v1/auth/password-reset/",
            {"email": self.user.email},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/reset-password?uid=", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(mail.outbox[0].alternatives[0].mimetype, "text/html")
        self.assertIn("cid:webster-logo", mail.outbox[0].alternatives[0].content)
        self.assertEqual(mail.outbox[0].attachments[0].get_content_id(), "<webster-logo>")

    def test_confirm_changes_password_and_token_cannot_be_reused(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        payload = {
            "uid": uid,
            "token": token,
            "new_password": "NewPassword123!",
            "confirm_password": "NewPassword123!",
        }

        response = self.client.post("/api/v1/auth/password-reset/confirm/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPassword123!"))

        response = self.client.post("/api/v1/auth/password-reset/confirm/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_email_returns_same_success_response_without_sending_email(self):
        response = self.client.post(
            "/api/v1/auth/password-reset/",
            {"email": "unknown@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(
    TELEGRAM_BOT_USERNAME="TaskFlowTestBot",
    TELEGRAM_BOT_TOKEN="test-token",
    TELEGRAM_WEBHOOK_SECRET="test-secret",
)
class TelegramIntegrationApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="telegram-user", email="telegram@example.com", password="pass12345"
        )

    def test_authenticated_user_gets_one_time_connect_link(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/v1/me/telegram/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("https://t.me/TaskFlowTestBot?start=", response.data["connect_url"])
        integration = TelegramIntegration.objects.get(user=self.user)
        self.assertIsNotNone(integration.link_token)
        self.assertGreater(integration.link_token_expires_at, timezone.now())

    @patch("apps.views.bot_api")
    def test_start_payload_connects_telegram_account(self, mocked_bot_api):
        integration = TelegramIntegration.objects.create(
            user=self.user,
            link_token="one-time-token",
            link_token_expires_at=timezone.now() + timedelta(minutes=15),
        )
        response = self.client.post(
            "/api/v1/telegram/webhook/",
            {"message": {"text": "/start one-time-token", "chat": {"id": 987}, "from": {"id": 654, "username": "tester"}}},
            format="json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        integration.refresh_from_db()
        self.assertTrue(integration.is_connected)
        self.assertEqual(integration.telegram_user_id, 654)
        self.assertIsNone(integration.link_token)
        mocked_bot_api.assert_called_once()

    def test_webhook_rejects_invalid_secret(self):
        response = self.client.post(
            "/api/v1/telegram/webhook/", {"message": {}}, format="json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong-secret",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


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

    @override_settings(
        TELEGRAM_SUPPORT_BOT_TOKEN="test-token",
        TELEGRAM_SUPPORT_CHAT_ID="-100123",
    )
    @patch("apps.views.send_support_message")
    def test_support_bot_sends_authenticated_user_details(self, send_message):
        self.owner.first_name = "Test"
        self.owner.last_name = "Owner"
        self.owner.save(update_fields=["first_name", "last_name"])

        response = self.client.post(
            "/api/v1/support/bot/",
            {"message": "The dashboard is not loading."},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kwargs = send_message.call_args.kwargs
        self.assertEqual(kwargs["token"], "test-token")
        self.assertEqual(kwargs["chat_id"], "-100123")
        self.assertIn("From: Test Owner", kwargs["text"])
        self.assertIn("Department: Engineering", kwargs["text"])
        self.assertIn("The dashboard is not loading.", kwargs["text"])
        self.assertIsNone(kwargs["screenshot"])

    @override_settings(TELEGRAM_SUPPORT_BOT_TOKEN="", TELEGRAM_SUPPORT_CHAT_ID="")
    def test_support_bot_reports_missing_configuration(self):
        response = self.client.post(
            "/api/v1/support/bot/",
            {"message": "Need help"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

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
        self.assertEqual(str(response.data["created_by"]), str(self.owner.pk))
        self.assertEqual(
            set(response.data["created_by_detail"]),
            {"id", "full_name", "avatar"},
        )

    def test_first_assignee_is_main_and_only_main_can_change_status(self):
        second = User.objects.create_user(
            username="second-assignee",
            email="second-assignee@example.com",
            password="pass12345",
            department=self.department,
        )
        response = self.client.post(
            "/api/v1/tasks/",
            {
                "department": self.department.pk,
                "title": "Ordered assignees",
                "assignees": [self.member.pk, second.pk],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(response.data["main_assignee"]), str(self.member.pk))
        task_id = response.data["id"]

        self.client.force_authenticate(second)
        response = self.client.patch(
            f"/api/v1/tasks/{task_id}/",
            {"status": Task.Status.IN_PROGRESS},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.owner)
        response = self.client.patch(
            f"/api/v1/tasks/{task_id}/",
            {"status": Task.Status.IN_PROGRESS},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.member)
        response = self.client.patch(
            f"/api/v1/tasks/{task_id}/",
            {"status": Task.Status.IN_PROGRESS, "progress": 25},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], Task.Status.IN_PROGRESS)
        self.assertEqual(response.data["progress"], 25)

    def test_task_department_is_inferred_from_first_assignee(self):
        other_department = Department.objects.create(name="Operations", code="operations-auto")
        main_assignee = User.objects.create_user(
            username="operations-main",
            email="operations-main@example.com",
            password="pass12345",
            department=other_department,
        )
        second_assignee = User.objects.create_user(
            username="engineering-second",
            email="engineering-second@example.com",
            password="pass12345",
            department=self.department,
        )
        self.owner.has_all_departments_access = True
        self.owner.save(update_fields=["has_all_departments_access"])

        response = self.client.post(
            "/api/v1/tasks/",
            {
                "title": "Department inferred from main assignee",
                "assignees": [main_assignee.pk, second_assignee.pk],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(response.data["department"]), str(other_department.pk))
        self.assertEqual(str(response.data["main_assignee"]), str(main_assignee.pk))

        self.client.force_authenticate(main_assignee)
        update_response = self.client.patch(
            f"/api/v1/tasks/{response.data['id']}/",
            {"status": Task.Status.IN_PROGRESS, "progress": 30},
            format="json",
        )

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data["status"], Task.Status.IN_PROGRESS)
        self.assertEqual(update_response.data["progress"], 30)

    def test_task_without_department_or_assignees_is_rejected(self):
        response = self.client.post(
            "/api/v1/tasks/",
            {"title": "Missing assignee"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("assignees", response.data["errors"])

    def test_hidden_task_is_visible_only_to_privileged_roles_and_assignees(self):
        assignee = self.member
        unassigned = User.objects.create_user(
            username="unassigned",
            email="unassigned@example.com",
            password="pass12345",
            department=self.department,
        )
        response = self.client.post(
            "/api/v1/tasks/",
            {
                "department": self.department.pk,
                "title": "Private launch",
                "is_hidden": True,
                "assignees": [assignee.pk],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_hidden"])
        task_id = response.data["id"]

        self.client.force_authenticate(assignee)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}/").status_code, status.HTTP_200_OK)

        self.client.force_authenticate(unassigned)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}/").status_code, status.HTTP_404_NOT_FOUND)

        for role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER):
            privileged = self.owner if role == User.Role.OWNER else User.objects.create_user(
                username=f"hidden-{role}",
                email=f"hidden-{role}@example.com",
                password="pass12345",
                department=self.department,
                role=role,
            )
            with self.subTest(role=role):
                self.client.force_authenticate(privileged)
                self.assertEqual(
                    self.client.get(f"/api/v1/tasks/{task_id}/").status_code,
                    status.HTTP_200_OK,
                )

    def test_hidden_task_is_excluded_from_member_analytics_and_dashboard(self):
        hidden = Task.objects.create(
            department=self.department,
            title="Confidential plan",
            created_by=self.owner,
            is_hidden=True,
        )
        visible = Task.objects.create(
            department=self.department,
            title="Public plan",
            created_by=self.owner,
        )
        visible.assignees.add(self.member)
        self.client.force_authenticate(self.member)

        analytics = self.client.get("/api/v1/analytics/", {"department": self.department.pk})
        dashboard = self.client.get("/api/v1/dashboard/")

        self.assertEqual(analytics.data["monthly_progress"][0]["created"], 1)
        self.assertEqual(dashboard.data["summary"]["total_tasks"]["count"], 1)
        self.assertNotIn(str(hidden.pk), {item["id"] for item in dashboard.data["recent_tasks"]})

    def test_member_dashboard_metrics_are_scoped_to_own_department(self):
        other_department = Department.objects.create(name="Dashboard Other", code="dashboard-other")
        Task.objects.create(
            department=self.department,
            title="Primary department task",
            created_by=self.owner,
        )
        Task.objects.create(
            department=other_department,
            title="Other department task",
            created_by=self.owner,
        )
        restricted_user = User.objects.create_user(
            username="dashboard-restricted",
            email="dashboard-restricted@example.com",
            password="pass12345",
            department=self.department,
            role=User.Role.MEMBER,
        )

        self.client.force_authenticate(self.owner)
        owner_dashboard = self.client.get("/api/v1/dashboard/")
        self.client.force_authenticate(restricted_user)
        restricted_dashboard = self.client.get("/api/v1/dashboard/")

        self.assertEqual(owner_dashboard.data["summary"]["total_tasks"]["count"], 2)
        self.assertEqual(restricted_dashboard.data["summary"]["total_tasks"]["count"], 1)
        self.assertEqual(len(restricted_dashboard.data["tasks_by_department"]), 1)
        self.assertEqual(restricted_dashboard.data["tasks_by_department"][0]["percentage"], 100.0)
        self.assertEqual(
            restricted_dashboard.data["tasks_by_department"][0]["department_id"],
            str(self.department.pk),
        )

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
                requester.accessible_departments.add(other_department)
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

    def test_manager_access_is_limited_to_selected_departments(self):
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

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get("/api/v1/departments/").data["count"], 1)

        manager.accessible_departments.add(other_department)
        response = self.client.post(
            "/api/v1/tasks/",
            {"department": other_department.pk, "title": "Prepare budget"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(response.data["department"]), str(other_department.pk))
        self.assertEqual(self.client.get("/api/v1/departments/").data["count"], 2)

    def test_owner_and_manager_with_all_department_access_need_no_primary_department(self):
        target_department = Department.objects.create(name="Strategy", code="strategy")
        attendee_department = Department.objects.create(name="Research", code="research")
        assignee = User.objects.create_user(
            username="all-access-assignee",
            email="all-access-assignee@example.com",
            password="pass12345",
            department=attendee_department,
        )
        starts_at = timezone.now() + timedelta(hours=1)

        for role in (User.Role.OWNER, User.Role.MANAGER):
            with self.subTest(role=role):
                requester = User.objects.create_user(
                    username=f"all-access-{role}",
                    email=f"all-access-{role}@example.com",
                    password="pass12345",
                    department=None,
                    role=role,
                    has_all_departments_access=True,
                )
                self.client.force_authenticate(requester)

                task_response = self.client.post(
                    "/api/v1/tasks/",
                    {
                        "department": target_department.pk,
                        "title": f"{role} company-wide task",
                        "assignees": [assignee.pk],
                    },
                    format="json",
                )
                event_response = self.client.post(
                    "/api/v1/events/",
                    {
                        "department": target_department.pk,
                        "title": f"{role} company-wide event",
                        "starts_at": starts_at,
                        "ends_at": starts_at + timedelta(hours=1),
                        "attendees": [assignee.pk],
                    },
                    format="json",
                )

                self.assertEqual(task_response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(event_response.status_code, status.HTTP_201_CREATED)
                self.assertTrue(
                    Task.objects.get(title=f"{role} company-wide task")
                    .assignees.filter(pk=assignee.pk)
                    .exists()
                )
                self.assertTrue(
                    Event.objects.get(title=f"{role} company-wide event")
                    .attendees.filter(pk=assignee.pk)
                    .exists()
                )

    def test_manager_sees_all_tasks_in_accessible_department(self):
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
            {"Assigned to manager", "Created by manager", "Unrelated task"},
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

    def test_member_search_uses_user_fields(self):
        self.member.first_name = "Aziza"
        self.member.last_name = "Karimova"
        self.member.job_title = "Backend Engineer"
        self.member.save(update_fields=["first_name", "last_name", "job_title"])

        for term in ("Aziza", "Karimova", "member@example.com", "Backend"):
            with self.subTest(term=term):
                response = self.client.get("/api/v1/members/", {"search": term})
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["count"], 1)
                self.assertEqual(response.data["results"][0]["id"], self.member.pk)

    def test_member_filters_and_ordering(self):
        inactive_admin = User.objects.create_user(
            username="inactive-admin",
            email="inactive-admin@example.com",
            password="pass12345",
            first_name="Amina",
            department=self.department,
            role=User.Role.ADMIN,
            is_active=False,
        )
        self.member.first_name = "Zafar"
        self.member.save(update_fields=["first_name"])
        self.owner.first_name = "Owner"
        self.owner.save(update_fields=["first_name"])

        cases = (
            ({"department": self.department.pk}, 3),
            ({"role": User.Role.ADMIN}, 1),
            ({"is_active": "false"}, 1),
            ({"department": self.department.pk, "role": User.Role.ADMIN, "is_active": "false"}, 1),
        )
        for params, expected_count in cases:
            with self.subTest(params=params):
                response = self.client.get("/api/v1/members/", params)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["count"], expected_count)

        response = self.client.get("/api/v1/members/", {"ordering": "first_name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["id"], inactive_admin.pk)

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

    def test_analytics_returns_chart_payload_and_applies_filters(self):
        done = Task.objects.create(
            department=self.department,
            title="Done on time",
            created_by=self.owner,
            status=Task.Status.COMPLETED,
            priority=Task.Priority.HIGH,
            due_date=timezone.localdate(),
            completed_at=timezone.now(),
        )
        done.assignees.add(self.member)
        overdue = Task.objects.create(
            department=self.department,
            title="Overdue high priority",
            created_by=self.owner,
            status=Task.Status.IN_PROGRESS,
            priority=Task.Priority.HIGH,
            due_date=timezone.localdate() - timedelta(days=3),
        )
        overdue.assignees.add(self.member)
        Task.objects.create(
            department=self.department,
            title="Excluded low priority",
            created_by=self.owner,
            priority=Task.Priority.LOW,
        )

        response = self.client.get("/api/v1/analytics/", {
            "department": self.department.pk,
            "employee": self.member.pk,
            "priority": Task.Priority.HIGH,
            "days": 30,
            "granularity": "day",
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["charts"]["task_status"]["total"], 2)
        self.assertEqual(response.data["summary"]["task_completion_rate"]["value"], 50.0)
        self.assertEqual(response.data["overdue"]["count"], 1)
        self.assertEqual(response.data["overdue"]["items"][0]["days_overdue"], 3)
        self.assertEqual(response.data["overdue"]["items"][0]["assignee"]["id"], str(self.member.pk))
        self.assertEqual(response.data["meta"]["applied_filters"]["priority"], Task.Priority.HIGH)

    def test_analytics_rejects_invalid_filters(self):
        response = self.client.get("/api/v1/analytics/", {"priority": "urgent"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get("/api/v1/analytics/", {
            "start_date": "2026-08-10", "end_date": "2026-08-01"
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dashboard_returns_english_sections(self):
        self.owner.first_name = "Dashboard"
        self.owner.last_name = "Owner"
        self.owner.save(update_fields=["first_name", "last_name"])
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
        Task.objects.create(
            department=self.department,
            title="Archived task",
            created_by=self.owner,
            status=Task.Status.COMPLETED,
            is_archived=True,
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
        self.assertEqual(response.data["summary"]["total_tasks"]["count"], 4)
        self.assertEqual(response.data["summary"]["archived_tasks"]["count"], 1)
        self.assertEqual(response.data["summary"]["archived_tasks"]["percentage"], 25.0)
        creator = response.data["recent_tasks"][0]["created_by_detail"]
        self.assertEqual(set(creator), {"id", "full_name", "avatar"})
        self.assertEqual(creator["full_name"], "Dashboard Owner")

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
                requester.accessible_departments.add(other_department)
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


class NotificationApiTests(APITestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Product", code="product")
        self.owner = User.objects.create_user(
            username="notification-owner", email="notification-owner@example.com",
            password="pass12345", department=self.department, role=User.Role.OWNER,
        )
        self.member = User.objects.create_user(
            username="notification-member", email="notification-member@example.com",
            password="pass12345", department=self.department,
        )
        self.other = User.objects.create_user(
            username="notification-other", email="notification-other@example.com",
            password="pass12345", department=self.department,
        )
        self.client.force_authenticate(self.owner)

    def test_task_assignment_creates_notification_and_respects_preference(self):
        response = self.client.post(
            "/api/v1/tasks/",
            {"department": self.department.pk, "title": "Ship release", "assignees": [self.member.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Notification.objects.filter(
            recipient=self.member, notification_type=Notification.Type.TASK_ASSIGNED
        ).exists())

        UserPreference.objects.update_or_create(user=self.other, defaults={"task_assigned": False})
        task = Task.objects.get(pk=response.data["id"])
        response = self.client.patch(
            f"/api/v1/tasks/{task.pk}/", {"assignees": [self.member.pk, self.other.pk]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Notification.objects.filter(recipient=self.other).exists())

    def test_completed_task_notifies_creator(self):
        task = Task.objects.create(
            department=self.department,
            title="Finish docs",
            created_by=self.owner,
            main_assignee=self.member,
        )
        task.assignees.add(self.member)
        self.client.force_authenticate(self.member)
        response = self.client.patch(
            f"/api/v1/tasks/{task.pk}/", {"status": Task.Status.COMPLETED}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Notification.objects.filter(
            recipient=self.owner, notification_type=Notification.Type.TASK_COMPLETED, task=task
        ).exists())

    def test_new_message_notifies_unmuted_participants(self):
        conversation = Conversation.objects.create(department=self.department, title="Release")
        ConversationParticipant.objects.create(conversation=conversation, user=self.owner)
        ConversationParticipant.objects.create(conversation=conversation, user=self.member)
        ConversationParticipant.objects.create(conversation=conversation, user=self.other, is_muted=True)
        conversation.participants.add(self.owner, self.member, self.other)
        response = self.client.post(
            "/api/v1/messages/", {"conversation": conversation.pk, "body": "Ready to ship"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Notification.objects.filter(recipient=self.member, message_id=response.data["id"]).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.other).exists())

    def test_notification_api_is_private_and_supports_read_actions(self):
        own = Notification.objects.create(
            recipient=self.owner, notification_type=Notification.Type.TASK_OVERDUE, title="Overdue"
        )
        Notification.objects.create(
            recipient=self.member, notification_type=Notification.Type.TASK_OVERDUE, title="Private"
        )
        response = self.client.get("/api/v1/notifications/")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/notifications/unread_count/").data["unread_count"], 1)
        response = self.client.post(f"/api/v1/notifications/{own.pk}/mark_read/")
        self.assertTrue(response.data["is_read"])
        Notification.objects.create(
            recipient=self.owner, notification_type=Notification.Type.TASK_OVERDUE, title="Another"
        )
        self.assertEqual(self.client.post("/api/v1/notifications/mark_all_read/").data["updated"], 1)

    def test_deadline_generation_is_idempotent(self):
        UserPreference.objects.update_or_create(user=self.member, defaults={"deadline_reminder": True})
        task = Task.objects.create(
            department=self.department, title="Deploy", created_by=self.owner,
            due_date=timezone.localdate() + timedelta(days=1),
        )
        task.assignees.add(self.member)
        self.assertEqual(generate_deadline_notifications(), 1)
        self.assertEqual(generate_deadline_notifications(), 0)
        self.assertEqual(Notification.objects.filter(
            recipient=self.member, notification_type=Notification.Type.DEADLINE_REMINDER
        ).count(), 1)
