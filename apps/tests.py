from datetime import timedelta

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase
from tablib import Dataset

from .forms import BulkMembershipAdminForm, EventAdminForm
from .models import Conversation, ConversationParticipant, Department, Event, Membership, Project, Report, Task, TimeEntry, User, Workspace
from .resources import DepartmentResource, UserResource


class TaskFlowAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sarah", email="sarah@example.com", password="StrongPass123")
        self.other = User.objects.create_user(username="mike", email="mike@example.com", password="StrongPass123")
        self.workspace = Workspace.objects.create(name="Acme", slug="acme", owner=self.user)
        self.department = Department.objects.create(
            workspace=self.workspace,
            name="Engineering",
            code="engineering",
        )
        Membership.objects.create(workspace=self.workspace, department=self.department, user=self.user, role=Membership.Role.OWNER)
        Membership.objects.create(workspace=self.workspace, department=self.department, user=self.other)
        self.project = Project.objects.create(workspace=self.workspace, department=self.department, name="Website", created_by=self.user, status=Project.Status.IN_PROGRESS)
        self.client.force_authenticate(self.user)

    def test_workspace_isolation(self):
        stranger = User.objects.create_user(username="stranger", email="stranger@example.com", password="StrongPass123")
        hidden = Workspace.objects.create(name="Hidden", slug="hidden", owner=stranger)
        response = self.client.get("/api/v1/workspaces/")
        ids = [row["id"] for row in response.data]
        self.assertIn(str(self.workspace.id), ids)
        self.assertNotIn(str(hidden.id), ids)

    def test_create_department_and_assign_member(self):
        department_response = self.client.post(
            "/api/v1/departments/",
            {
                "workspace": str(self.workspace.id),
                "name": "Admissions",
                "code": "admissions",
                "description": "Student admissions office",
            },
            format="json",
        )
        self.assertEqual(department_response.status_code, status.HTTP_201_CREATED)
        department = Department.objects.get(pk=department_response.data["id"])

        membership = Membership.objects.get(workspace=self.workspace, user=self.other)
        membership_response = self.client.patch(
            f"/api/v1/members/{membership.id}/",
            {"department": str(department.id)},
            format="json",
        )
        self.assertEqual(membership_response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertEqual(membership.department, department)

    def test_owner_can_add_member_to_own_department(self):
        new_user = User.objects.create_user(
            username="new-member",
            email="new-member@example.com",
            password="StrongPass123",
        )

        response = self.client.post(
            "/api/v1/members/",
            {
                "workspace": str(self.workspace.pk),
                "department": str(self.department.pk),
                "user": new_user.pk,
                "role": Membership.Role.MEMBER,
                "is_active": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            Membership.objects.filter(
                workspace=self.workspace,
                department=self.department,
                user=new_user,
                role=Membership.Role.MEMBER,
            ).exists()
        )

    def test_member_cannot_add_member(self):
        new_user = User.objects.create_user(
            username="blocked-member",
            email="blocked-member@example.com",
            password="StrongPass123",
        )
        self.client.force_authenticate(self.other)

        response = self.client.post(
            "/api/v1/members/",
            {
                "workspace": str(self.workspace.pk),
                "department": str(self.department.pk),
                "user": new_user.pk,
                "role": Membership.Role.MEMBER,
                "is_active": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            Membership.objects.filter(workspace=self.workspace, user=new_user).exists()
        )

    def test_manager_cannot_add_member_to_another_department(self):
        manager = User.objects.create_user(
            username="department-manager",
            email="department-manager@example.com",
            password="StrongPass123",
        )
        new_user = User.objects.create_user(
            username="cross-department-member",
            email="cross-department-member@example.com",
            password="StrongPass123",
        )
        other_department = Department.objects.create(
            workspace=self.workspace,
            name="Finance",
            code="finance",
        )
        Membership.objects.create(
            workspace=self.workspace,
            department=self.department,
            user=manager,
            role=Membership.Role.MANAGER,
        )
        self.client.force_authenticate(manager)

        response = self.client.post(
            "/api/v1/members/",
            {
                "workspace": str(self.workspace.pk),
                "department": str(other_department.pk),
                "user": new_user.pk,
                "role": Membership.Role.MEMBER,
                "is_active": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_add_member_to_own_department(self):
        manager = User.objects.create_user(
            username="own-department-manager",
            email="own-department-manager@example.com",
            password="StrongPass123",
        )
        new_user = User.objects.create_user(
            username="manager-added-member",
            email="manager-added-member@example.com",
            password="StrongPass123",
        )
        Membership.objects.create(
            workspace=self.workspace,
            department=self.department,
            user=manager,
            role=Membership.Role.MANAGER,
        )
        self.client.force_authenticate(manager)

        response = self.client.post(
            "/api/v1/members/",
            {
                "workspace": str(self.workspace.pk),
                "department": str(self.department.pk),
                "user": new_user.pk,
                "role": Membership.Role.MEMBER,
                "is_active": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_membership_rejects_department_from_another_workspace(self):
        other_workspace = Workspace.objects.create(name="Other", slug="other", owner=self.user)
        other_department = Department.objects.create(
            workspace=other_workspace,
            name="Finance",
            code="finance",
        )
        membership = Membership.objects.get(workspace=self.workspace, user=self.other)
        response = self.client.patch(
            f"/api/v1/members/{membership.id}/",
            {"department": str(other_department.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_department_resource_imports_workspace_by_name(self):
        dataset = Dataset(
            [self.workspace.name, "Academic Affairs", "", "", True],
            headers=["workspace", "name", "code", "description", "is_active"],
        )
        result = DepartmentResource().import_data(dataset, dry_run=False, raise_errors=True)

        self.assertFalse(result.has_errors())
        department = Department.objects.get(workspace=self.workspace, name="Academic Affairs")
        self.assertEqual(department.code, "academic-affairs")

    def test_create_and_complete_task(self):
        payload = {"project": str(self.project.id), "title": "Build API", "priority": "high", "assignees": [self.other.id], "progress": 50}
        created = self.client.post("/api/v1/tasks/", payload, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        updated = self.client.patch(f"/api/v1/tasks/{created.data['id']}/", {"status": "completed"}, format="json")
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        task = Task.objects.get(id=created.data["id"])
        self.assertEqual(task.progress, 100)
        self.assertIsNotNone(task.completed_at)

    def test_manager_can_edit_task_but_member_cannot(self):
        manager = User.objects.create_user(
            username="task-manager",
            email="task-manager@example.com",
            password="StrongPass123",
        )
        Membership.objects.create(
            workspace=self.workspace,
            department=self.department,
            user=manager,
            role=Membership.Role.MANAGER,
        )
        task = Task.objects.create(
            project=self.project,
            title="Manager task",
            created_by=self.user,
        )

        self.client.force_authenticate(manager)
        manager_response = self.client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Edited by manager"},
            format="json",
        )
        self.assertEqual(manager_response.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(self.other)
        member_response = self.client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Edited by member"},
            format="json",
        )
        self.assertEqual(member_response.status_code, status.HTTP_404_NOT_FOUND)

        task.refresh_from_db()
        self.assertEqual(task.title, "Edited by manager")

    def test_member_can_update_status_and_progress_of_assigned_task(self):
        task = Task.objects.create(
            project=self.project,
            title="Member task",
            created_by=self.user,
        )
        task.assignees.add(self.other)
        self.client.force_authenticate(self.other)

        allowed = self.client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"status": Task.Status.IN_PROGRESS, "progress": 30},
            format="json",
        )
        forbidden = self.client.patch(
            f"/api/v1/tasks/{task.pk}/",
            {"title": "Member changed the title"},
            format="json",
        )

        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        self.assertEqual(forbidden.status_code, status.HTTP_403_FORBIDDEN)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)
        self.assertEqual(task.progress, 30)
        self.assertEqual(task.title, "Member task")

    def test_my_tasks_filter_returns_only_assigned_tasks(self):
        assigned = Task.objects.create(
            project=self.project,
            title="Assigned",
            created_by=self.user,
        )
        assigned.assignees.add(self.other)
        Task.objects.create(
            project=self.project,
            title="Not assigned",
            created_by=self.user,
        )
        self.client.force_authenticate(self.other)

        response = self.client.get("/api/v1/tasks/?my_tasks=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(assigned.pk))

    def test_member_task_list_always_returns_only_assigned_tasks(self):
        assigned = Task.objects.create(
            project=self.project,
            title="Assigned",
            created_by=self.user,
        )
        assigned.assignees.add(self.other)
        Task.objects.create(
            project=self.project,
            title="Not assigned",
            created_by=self.user,
        )
        self.client.force_authenticate(self.other)

        response = self.client.get("/api/v1/tasks/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(assigned.pk))

    def test_department_access_isolation_for_projects_and_tasks(self):
        other_department = Department.objects.create(
            workspace=self.workspace,
            name="Finance",
            code="finance",
        )
        other_manager = User.objects.create_user(
            username="finance-manager",
            email="finance-manager@example.com",
            password="StrongPass123",
        )
        Membership.objects.create(
            workspace=self.workspace,
            department=other_department,
            user=other_manager,
            role=Membership.Role.MANAGER,
        )
        hidden_project = Project.objects.create(
            workspace=self.workspace,
            department=other_department,
            name="Budget",
            created_by=other_manager,
        )
        hidden_task = Task.objects.create(
            project=hidden_project,
            title="Prepare budget",
            created_by=other_manager,
        )

        projects = self.client.get("/api/v1/projects/")
        tasks = self.client.get("/api/v1/tasks/")

        self.assertNotIn(str(hidden_project.pk), [row["id"] for row in projects.data["results"]])
        self.assertNotIn(str(hidden_task.pk), [row["id"] for row in tasks.data["results"]])

    def test_member_cannot_manage_project(self):
        self.client.force_authenticate(self.other)

        updated = self.client.patch(
            f"/api/v1/projects/{self.project.pk}/",
            {"name": "Changed by member"},
            format="json",
        )
        deleted = self.client.delete(f"/api/v1/projects/{self.project.pk}/")

        self.assertEqual(updated.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(deleted.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_can_start_and_stop_timer_for_assigned_task(self):
        task = Task.objects.create(
            project=self.project,
            title="Timed task",
            created_by=self.user,
        )
        task.assignees.add(self.other)
        self.client.force_authenticate(self.other)

        started = self.client.post(f"/api/v1/tasks/{task.pk}/start-timer/")
        duplicate = self.client.post(f"/api/v1/tasks/{task.pk}/start-timer/")
        stopped = self.client.post(f"/api/v1/tasks/{task.pk}/stop-timer/")

        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertEqual(duplicate.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(stopped.status_code, status.HTTP_200_OK)
        entry = TimeEntry.objects.get(pk=started.data["id"])
        self.assertIsNotNone(entry.ended_at)
        self.assertGreaterEqual(entry.minutes, 0)

    def test_calendar_rejects_invalid_time_range(self):
        now = timezone.now()
        payload = {"workspace": str(self.workspace.id), "title": "Review", "starts_at": now.isoformat(), "ends_at": (now - timedelta(hours=1)).isoformat()}
        response = self.client.post("/api/v1/events/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_event_accepts_custom_event_type(self):
        now = timezone.now()
        payload = {
            "workspace": str(self.workspace.id),
            "title": "University ceremony",
            "event_type": "Ceremony",
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(hours=1)).isoformat(),
        }

        response = self.client.post("/api/v1/events/", payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = Event.objects.get(pk=response.data["id"])
        self.assertEqual(event.event_type, "Ceremony")

    def test_event_admin_form_shows_suggestions_and_accepts_custom_type(self):
        now = timezone.now()
        form = EventAdminForm(
            data={
                "workspace": self.workspace.pk,
                "title": "Workshop",
                "event_type": "Workshop",
                "starts_at": now,
                "ends_at": now + timedelta(hours=1),
                "created_by": self.user.pk,
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("datalist", str(form["event_type"]))
        self.assertIn("Meeting", str(form["event_type"]))

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

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def disabled_password_reset_flow(self):
        """Enable this test when password-reset URLs are enabled."""
        self.client.force_authenticate(user=None)
        requested = self.client.post(
            "/api/v1/auth/password-reset/",
            {"email": self.user.email},
            format="json",
        )
        self.assertEqual(requested.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        confirmed = self.client.post(
            "/api/v1/auth/password-reset/confirm/",
            {
                "uid": uid,
                "token": token,
                "new_password": "ResetStrongPass456",
                "confirm_password": "ResetStrongPass456",
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("ResetStrongPass456"))

    def test_public_registration_is_disabled(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(
            "/api/v1/auth/register/",
            {"email": "new@example.com", "username": "new-user", "password": "StrongPass123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_import_formats_include_xlsx(self):
        self.assertIn("xlsx", [file_format().get_extension() for file_format in settings.IMPORT_FORMATS])

    def test_user_resource_imports_but_does_not_export_password(self):
        resource = UserResource()
        self.assertEqual(resource._meta.import_id_fields, ("email",))
        self.assertIn("password", [field.column_name for field in resource.get_import_fields()])
        self.assertNotIn("password", [field.column_name for field in resource.get_export_fields()])

    def test_unsaved_time_entry_has_zero_minutes(self):
        entry = TimeEntry()
        self.assertEqual(entry.minutes, 0)

    def test_bulk_membership_admin_form_assigns_multiple_users_to_each_role(self):
        department = Department.objects.create(
            workspace=self.workspace,
            name="Admissions",
            code="admissions",
        )
        users = [
            User.objects.create_user(
                username=f"bulk-{index}",
                email=f"bulk-{index}@example.com",
                password="StrongPass123",
            )
            for index in range(6)
        ]
        form = BulkMembershipAdminForm(
            data={
                "workspace": self.workspace.pk,
                "department": department.pk,
                "owners": [users[0].pk],
                "admins": [users[1].pk],
                "managers": [users[2].pk, users[3].pk],
                "members": [users[4].pk, users[5].pk],
                "is_active": True,
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form.save_remaining_memberships()

        created = Membership.objects.filter(
            workspace=self.workspace,
            department=department,
            user__in=users,
        )
        self.assertEqual(created.count(), 6)
        self.assertEqual(
            created.filter(role=Membership.Role.MANAGER).count(),
            2,
        )
        self.assertEqual(
            created.filter(role=Membership.Role.MEMBER).count(),
            2,
        )

    def test_bulk_membership_admin_form_rejects_user_in_multiple_roles(self):
        user = User.objects.create_user(
            username="duplicate-role",
            email="duplicate-role@example.com",
            password="StrongPass123",
        )
        form = BulkMembershipAdminForm(
            data={
                "workspace": self.workspace.pk,
                "owners": [user.pk],
                "admins": [user.pk],
                "is_active": True,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("already selected", str(form.errors))

    def test_bulk_membership_admin_form_updates_existing_membership(self):
        department = Department.objects.create(
            workspace=self.workspace,
            name="Operations",
            code="operations",
        )
        form = BulkMembershipAdminForm(
            data={
                "workspace": self.workspace.pk,
                "department": department.pk,
                "managers": [self.other.pk],
                "is_active": True,
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form.save_remaining_memberships()

        membership = Membership.objects.get(
            workspace=self.workspace,
            user=self.other,
        )
        self.assertEqual(membership.department, department)
        self.assertEqual(membership.role, Membership.Role.MANAGER)

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
