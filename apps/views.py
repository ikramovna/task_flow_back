import csv
import io
import secrets
from datetime import date, datetime, time, timedelta
from email.mime.image import MIMEImage

from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string
from django.http import FileResponse
from django.db import transaction
from django.db.models import Avg, Count, F, Prefetch, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer

from .filters import EventFilter, TaskFilter
from .models import Conversation, ConversationParticipant, Department, Event, Message, Notification, Project, Report, Task, TelegramIntegration, User, UserPreference
from .chat.services import create_message
from .notifications import notify_task_assigned, notify_task_completed
from .pagination import StandardPagination
from .permissions import IsDepartmentMember
from .serializers import AccountDeleteSerializer, ConversationSerializer, DepartmentSerializer, EventSerializer, MemberSerializer, MessageSerializer, NotificationSerializer, PasswordChangeSerializer, PasswordResetConfirmSerializer, PasswordResetRequestSerializer, ProfileSerializer, ProjectSerializer, ReportSerializer, SupportBotMessageSerializer, TaskCreatorSerializer, TaskSerializer, TwoFactorSerializer, UserBriefSerializer, UserPreferenceSerializer
from .telegram_support import TelegramSupportError, send_support_message
from .telegram import TelegramError, bot_api, webhook_url
from .task_visibility import PRIVILEGED_TASK_ROLES, visible_tasks_for


class PasswordResetRequestView(generics.GenericAPIView):
    serializer_class = PasswordResetRequestSerializer
    permission_classes = (AllowAny,)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email__iexact=serializer.validated_data["email"],
            is_active=True,
        ).first()

        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?uid={uid}&token={token}"
            display_name = user.get_full_name() or user.email
            text_body = (
                f"Hello {display_name},\n\n"
                "We received a request to reset the password for your TaskFlow account. "
                "Use the following link to set a new password:\n"
                f"{reset_url}\n\n"
                "This link will expire in 1 hour.\n\n"
                "If you did not request a password reset, you can safely ignore this email."
            )
            html_body = render_to_string("emails/password_reset.html", {
                "display_name": display_name,
                "reset_url": reset_url,
            })
            email = EmailMultiAlternatives(
                subject="Reset your TaskFlow password",
                body=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.email],
            )
            email.attach_alternative(html_body, "text/html")
            logo_path = settings.BASE_DIR / "templates" / "emails" / "assets" / "webster-tashkent-logo.png"
            with logo_path.open("rb") as logo_file:
                logo = MIMEImage(logo_file.read(), _subtype="png")
            logo.add_header("Content-ID", "<webster-logo>")
            logo.add_header("Content-Disposition", "inline", filename="webster-tashkent-logo.png")
            email.attach(logo)
            email.send()

        return Response({
            "detail": "If an account with this email exists, a password reset link has been sent."
        })


class PasswordResetConfirmView(generics.GenericAPIView):
    serializer_class = PasswordResetConfirmSerializer
    permission_classes = (AllowAny,)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        if not default_token_generator.check_token(user, serializer.validated_data["token"]):
            return Response(
                {"token": "Reset link is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password reset successfully."})


class DepartmentScopedMixin:
    permission_classes = (IsAuthenticated, IsDepartmentMember)
    pagination_class = StandardPagination

    def department_id(self):
        return self.request.query_params.get("department")

    def ensure_member(self, department):
        user = self.request.user
        if not user.is_active or not user.can_access_department(department):
            raise serializers.ValidationError({"department": "You are not a member of this department."})

    def scope_departments(self, queryset, field="department"):
        user = self.request.user
        if user.is_superuser or user.has_all_departments_access:
            return queryset
        return queryset.filter(
            Q(**{f"{field}_id": user.department_id})
            | Q(**{f"{field}__users_with_access": user})
        ).distinct()


class MemberViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = User.objects.none()
    serializer_class = MemberSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_fields = ("department", "role", "is_active")
    search_fields = ("first_name", "last_name", "email", "job_title")
    ordering_fields = ("date_joined", "first_name", "last_name", "email", "job_title", "role", "is_active")
    ordering = ("date_joined",)

    manager_roles = (
        User.Role.OWNER,
        User.Role.ADMIN,
        User.Role.MANAGER,
    )

    def can_manage(self, department):
        current = self.request.user
        return current.is_superuser or (
            current.is_active
            and current.role in self.manager_roles
            and current.can_access_department(department)
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        task_visibility = Q()
        if not self.request.user.is_superuser and self.request.user.role not in PRIVILEGED_TASK_ROLES:
            task_visibility = Q(assigned_tasks__is_hidden=False) | Q(assigned_tasks__assignees=self.request.user)
        qs = User.objects.select_related("department").annotate(
            task_count=Count(
                "assigned_tasks",
                filter=Q(assigned_tasks__department_id=F("department_id")) & task_visibility,
                distinct=True,
            ),
            completed_task_count=Count(
                "assigned_tasks",
                filter=Q(
                    assigned_tasks__department_id=F("department_id"),
                    assigned_tasks__status=Task.Status.COMPLETED,
                ) & task_visibility,
                distinct=True,
            ),
            in_progress_task_count=Count(
                "assigned_tasks",
                filter=Q(
                    assigned_tasks__department_id=F("department_id"),
                    assigned_tasks__status=Task.Status.IN_PROGRESS,
                ) & task_visibility,
                distinct=True,
            ),
        )
        if not self.request.user.is_superuser:
            qs = self.scope_departments(qs)
        return qs.filter(department_id=self.department_id()) if self.department_id() else qs

    def perform_create(self, serializer):
        department = serializer.validated_data["department"]
        if not self.can_manage(department):
            raise PermissionDenied(
                "Only an Owner, Admin, or Manager of this department can add members."
            )
        serializer.save()

    def perform_update(self, serializer):
        department = serializer.validated_data.get("department", serializer.instance.department)
        if not self.can_manage(department):
            raise PermissionDenied("Only an Owner, Admin, or Manager of this department can update members.")
        serializer.save()

    def perform_destroy(self, instance):
        if not self.can_manage(instance.department):
            raise PermissionDenied("Only an Owner, Admin, or Manager of this department can remove members.")
        instance.delete()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.filter_queryset(self.get_queryset())
        tasks = visible_tasks_for(Task.objects.filter(assignees__in=qs), request.user).distinct()
        total = qs.count()
        completed = tasks.filter(status=Task.Status.COMPLETED).count()
        efficiency = round(completed * 100 / tasks.count()) if tasks.exists() else 0
        return Response({"total_members": total, "average_efficiency": efficiency, "active_tasks": tasks.exclude(status=Task.Status.COMPLETED).count()})


class DepartmentViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Department.objects.none()
    serializer_class = DepartmentSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_fields = ("is_active",)
    search_fields = ("name", "code", "description")
    ordering_fields = ("name", "code", "created_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Department.objects.annotate(
            member_count=Count("users", filter=Q(users__is_active=True))
        ).order_by("name").distinct()
        if self.request.user.is_superuser or self.request.user.has_all_departments_access:
            return qs
        return qs.filter(Q(pk=self.request.user.department_id) | Q(users_with_access=self.request.user)).distinct()

    def perform_create(self, serializer):
        if not (self.request.user.is_superuser or self.request.user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)):
            raise PermissionDenied("Only an Owner, Admin, or Manager can create departments.")
        department = serializer.save()
        if not self.request.user.is_superuser and not self.request.user.has_all_departments_access:
            self.request.user.accessible_departments.add(department)

    def perform_update(self, serializer):
        if not (self.request.user.is_superuser or (self.request.user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER) and self.request.user.can_access_department(serializer.instance))):
            raise PermissionDenied("Only an Owner, Admin, or Manager can update departments.")
        serializer.save()

    def perform_destroy(self, instance):
        if not (self.request.user.is_superuser or (self.request.user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER) and self.request.user.can_access_department(instance))):
            raise PermissionDenied("Only an Owner, Admin, or Manager can delete departments.")
        instance.delete()


class ProjectViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Project.objects.none()
    serializer_class = ProjectSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_fields = ("department", "status", "priority", "category", "manager", "team_members")
    search_fields = ("name", "description", "category", "manager__first_name", "manager__last_name")
    ordering_fields = ("name", "start_date", "end_date", "priority", "status", "created_at", "updated_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        queryset = Project.objects.select_related(
            "department", "manager", "created_by"
        ).prefetch_related("team_members").annotate(
            task_count=Count("tasks", distinct=True),
            completed_task_count=Count(
                "tasks",
                filter=Q(tasks__status=Task.Status.COMPLETED),
                distinct=True,
            ),
            task_progress=Avg("tasks__progress"),
        )
        queryset = self.scope_departments(queryset)
        if self.department_id():
            queryset = queryset.filter(department_id=self.department_id())
        return queryset.distinct()

    def can_manage(self, department):
        user = self.request.user
        return user.is_superuser or (
            user.is_active
            and user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)
            and user.can_access_department(department)
        )

    def perform_create(self, serializer):
        department = serializer.validated_data["department"]
        if not self.can_manage(department):
            raise PermissionDenied("Only an Owner, Admin, or Manager can create projects.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        department = serializer.validated_data.get("department", serializer.instance.department)
        if not self.can_manage(department):
            raise PermissionDenied("Only an Owner, Admin, or Manager can update projects.")
        serializer.save()

    def perform_destroy(self, instance):
        if not self.can_manage(instance.department):
            raise PermissionDenied("Only an Owner, Admin, or Manager can delete projects.")
        instance.delete()


class TaskViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Task.objects.none()
    serializer_class = TaskSerializer
    # Creation permissions are checked in perform_create so managers can assign
    # tasks outside their normal department scope. Reads remain queryset-scoped.
    permission_classes = (IsAuthenticated,)
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_class = TaskFilter
    search_fields = ("title", "description", "department__name", "project__name")
    ordering_fields = ("title", "due_date", "priority", "status", "created_at", "progress")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        user = self.request.user
        if user.role in PRIVILEGED_TASK_ROLES:
            department_task_ids = self.scope_departments(
                Task.objects.all()
            ).values("pk")
            qs = Task.objects.filter(
                Q(pk__in=department_task_ids) | Q(created_by=user)
            )
        else:
            assigned_task_ids = Task.objects.filter(assignees=user).values("pk")
            qs = Task.objects.filter(
                Q(department=user.department) | Q(pk__in=assigned_task_ids)
            )
        qs = visible_tasks_for(qs, user)
        qs = qs.select_related("department", "project", "created_by", "main_assignee").prefetch_related("assignees").distinct()
        archived = self.request.query_params.get("archived", "").lower()
        if self.action == "unarchive":
            qs = qs.filter(is_archived=True)
        elif archived in ("1", "true", "yes"):
            qs = qs.filter(is_archived=True)
        elif archived != "all":
            qs = qs.filter(is_archived=False)
        if self.department_id():
            qs = qs.filter(department_id=self.department_id())
        if self.request.query_params.get("my_tasks", "").lower() in ("1", "true", "yes"):
            qs = qs.filter(assignees=self.request.user)
        return qs

    def perform_create(self, serializer):
        department = serializer.validated_data.get("department")
        if department is None:
            raise serializers.ValidationError({
                "assignees": "Select at least one assignee with a department."
            })
        self.ensure_department_task_manager(department)
        task = serializer.save(created_by=self.request.user)
        notify_task_assigned(task, self.request.user, task.assignees.all())

    @action(detail=False, methods=["get"], url_path="assignees")
    def assignees(self, request):
        """Search company-wide assignees exclusively for task assignment."""
        user = request.user
        if not (
            user.is_active
            and (
                user.is_superuser
                or user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)
            )
        ):
            raise PermissionDenied(
                "Only an Owner, Admin, or Manager can search task assignees."
            )

        search = request.query_params.get("search", "").strip()
        queryset = User.objects.none()
        if search:
            queryset = (
                User.objects.filter(is_active=True, department__isnull=False)
                .filter(
                    Q(first_name__icontains=search)
                    | Q(last_name__icontains=search)
                    | Q(email__icontains=search)
                    | Q(job_title__icontains=search)
                    | Q(department__name__icontains=search)
                )
                .select_related("department")
                .order_by("first_name", "last_name", "email")
                .distinct()
            )

        page = self.paginate_queryset(queryset)
        serializer = UserBriefSerializer(
            page if page is not None else queryset,
            many=True,
            context=self.get_serializer_context(),
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def ensure_department_task_manager(self, department):
        user = self.request.user
        can_manage = (
            user.is_active
            and user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)
        )
        if not can_manage:
            raise PermissionDenied(
                "Only an Owner, Admin, or Manager can manage tasks."
            )

    def ensure_task_manager(self, task):
        self.ensure_department_task_manager(task.department)

    def perform_update(self, serializer):
        task = serializer.instance
        target_department = serializer.validated_data.get("department", task.department)
        user = self.request.user
        is_manager = user.role in PRIVILEGED_TASK_ROLES
        if not user.is_active or (
            not is_manager and not user.can_access_department(target_department)
        ):
            raise PermissionDenied("You are not a member of this department.")

        changed_fields = set(serializer.validated_data)
        member_fields = {"status", "progress"}
        if changed_fields & member_fields and task.main_assignee_id != user.pk:
            raise PermissionDenied(
                "Only the main assignee can update task status and progress."
            )
        if not is_manager:
            if not task.assignees.filter(pk=self.request.user.pk).exists():
                raise PermissionDenied("You can update only tasks assigned to you.")
            if not changed_fields.issubset(member_fields):
                raise PermissionDenied(
                    "The main assignee can update only status and progress."
                )

        previous = serializer.instance.status
        previous_assignee_ids = set(serializer.instance.assignees.values_list("id", flat=True))
        task = serializer.save()
        new_assignees = task.assignees.exclude(id__in=previous_assignee_ids)
        notify_task_assigned(task, self.request.user, new_assignees)
        if task.status == Task.Status.COMPLETED and previous != Task.Status.COMPLETED:
            task.progress, task.completed_at = 100, timezone.now()
            task.save(update_fields=["progress", "completed_at", "updated_at"])
            notify_task_completed(task, self.request.user)
        elif task.status != Task.Status.COMPLETED and previous == Task.Status.COMPLETED:
            task.completed_at = None
            task.save(update_fields=["completed_at", "updated_at"])

    def perform_destroy(self, instance):
        self.ensure_task_manager(instance)
        instance.delete()

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        task = self.get_object()
        self.ensure_task_manager(task)
        if task.status != Task.Status.COMPLETED:
            return Response(
                {"detail": "Only completed tasks can be archived."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not task.is_archived:
            task.is_archived = True
            task.archived_at = timezone.now()
            task.archived_by = request.user
            task.save(update_fields=["is_archived", "archived_at", "archived_by", "updated_at"])
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=["post"])
    def unarchive(self, request, pk=None):
        task = self.get_object()
        self.ensure_task_manager(task)
        task.is_archived = False
        task.archived_at = None
        task.archived_by = None
        task.save(update_fields=["is_archived", "archived_at", "archived_by", "updated_at"])
        return Response(self.get_serializer(task).data)

class EventViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Event.objects.none()
    serializer_class = EventSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_class = EventFilter
    search_fields = ("title", "description", "location")
    ordering_fields = ("starts_at", "ends_at", "created_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Event.objects.select_related("department", "created_by").prefetch_related("attendees").distinct()
        qs = self.scope_departments(qs)
        return qs.filter(department_id=self.department_id()) if self.department_id() else qs

    def perform_create(self, serializer):
        self.ensure_member(serializer.validated_data["department"])
        serializer.save(created_by=self.request.user)


class AnalyticsView(APIView):
    permission_classes = (IsAuthenticated, IsDepartmentMember)

    @extend_schema(
        parameters=[
            OpenApiParameter("department", OpenApiTypes.UUID, OpenApiParameter.QUERY, required=False,
                             description="Department UUID. Omit to include every accessible department."),
            OpenApiParameter("employee", OpenApiTypes.UUID, OpenApiParameter.QUERY, required=False,
                             description="Employee UUID from the selected accessible department(s)."),
            OpenApiParameter("priority", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             enum=Task.Priority.values),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             enum=Task.Status.values),
            OpenApiParameter("days", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False,
                             enum=[7, 30, 90, 365], default=30,
                             description="Preset period. Ignored when start_date is supplied."),
            OpenApiParameter("start_date", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=False,
                             description="Custom period start (YYYY-MM-DD)."),
            OpenApiParameter("end_date", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=False,
                             description="Period end (YYYY-MM-DD); defaults to today."),
            OpenApiParameter("granularity", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             enum=["day", "week", "month"],
                             description="Trend bucket size; selected automatically when omitted."),
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             description="Search staff by name, email, username or job title."),
            OpenApiParameter("staff_search", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             description="Search only inside the Staff Performance table."),
            OpenApiParameter("performance_level", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             enum=["outstanding", "excellent", "good", "needs_improvement", "critical", "not_rated"]),
            OpenApiParameter("staff_filter", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                             enum=["all", "top_performers", "needs_attention"], default="all"),
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False, default=1),
            OpenApiParameter("page_size", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False, default=8),
        ],
        responses=inline_serializer(
            name="AnalyticsResponse",
            fields={"meta": serializers.JSONField(), "summary": serializers.JSONField(),
                    "charts": serializers.JSONField(), "staff_performance": serializers.JSONField(),
                    "overdue": serializers.JSONField(),
                    "task_completion_rate": serializers.FloatField(),
                    "team_velocity": serializers.IntegerField(),
                    "overdue_tasks": serializers.IntegerField(),
                    "monthly_progress": serializers.ListField(),
                    "tasks_by_category": serializers.ListField()},
        ),
    )
    def get(self, request):
        params = request.query_params
        today = timezone.localdate()

        try:
            end_date = date.fromisoformat(params.get("end_date", "")) if params.get("end_date") else today
            if params.get("start_date"):
                start_date = date.fromisoformat(params["start_date"])
            else:
                days = int(params.get("days", 30))
                if days not in (7, 30, 90, 365):
                    raise ValueError
                start_date = end_date - timedelta(days=days - 1)
            if start_date > end_date:
                raise ValueError
        except (TypeError, ValueError):
            return Response(
                {"detail": "Use valid start_date/end_date (YYYY-MM-DD), or days=7,30,90,365."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        departments = Department.objects.filter(is_active=True)
        if not request.user.is_superuser and not request.user.has_all_departments_access:
            departments = departments.filter(
                Q(pk=request.user.department_id) | Q(users_with_access=request.user)
            ).distinct()
        department_id = params.get("department")
        if department_id:
            try:
                department_allowed = departments.filter(pk=department_id).exists()
            except (TypeError, ValueError):
                department_allowed = False
            if not department_allowed:
                return Response({"detail": "You are not a member of this department."}, status=status.HTTP_403_FORBIDDEN)
            departments = departments.filter(pk=department_id)

        employees = User.objects.filter(is_active=True, department__in=departments).distinct().order_by("first_name", "last_name")
        if request.user.role not in PRIVILEGED_TASK_ROLES:
            employees = employees.filter(pk=request.user.pk)
        employee_options = employees
        search = params.get("search", "").strip()
        if search:
            employees = employees.filter(
                Q(first_name__icontains=search) | Q(last_name__icontains=search)
                | Q(email__icontains=search) | Q(username__icontains=search)
                | Q(job_title__icontains=search)
            )

        # Archived tasks are hidden from the operational Kanban, but analytics is
        # historical: verified work must remain visible in the selected period.
        universe = visible_tasks_for(
            Task.objects.filter(department__in=departments), request.user
        )
        if request.user.role not in PRIVILEGED_TASK_ROLES:
            universe = universe.filter(assignees=request.user)

        employee_id = params.get("employee")
        priority = params.get("priority")
        task_status = params.get("status")
        if employee_id:
            try:
                employee_exists = employee_options.filter(pk=employee_id).exists()
            except (TypeError, ValueError):
                employee_exists = False
            if not employee_exists:
                return Response({"employee": "Employee is not available in the selected department(s)."}, status=status.HTTP_400_BAD_REQUEST)
            universe = universe.filter(Q(assignees__id=employee_id) | Q(main_assignee_id=employee_id))
            employees = employees.filter(pk=employee_id)
        elif search:
            universe = universe.filter(Q(assignees__in=employees) | Q(main_assignee__in=employees))
        if priority:
            if priority not in Task.Priority.values:
                return Response({"priority": "Invalid priority."}, status=status.HTTP_400_BAD_REQUEST)
            universe = universe.filter(priority=priority)
        if task_status:
            if task_status not in Task.Status.values:
                return Response({"status": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
            universe = universe.filter(status=task_status)
        universe = universe.distinct()

        period_days = (end_date - start_date).days + 1
        previous_end = start_date - timedelta(days=1)
        previous_start = previous_end - timedelta(days=period_days - 1)
        # A task belongs to a period when it existed during that period. This keeps
        # older active/overdue work in the report instead of counting only newly
        # created tasks.
        current = universe.filter(created_at__date__lte=end_date).filter(
            Q(completed_at__isnull=True) | Q(completed_at__date__gte=start_date)
        )
        previous = universe.filter(created_at__date__lte=previous_end).filter(
            Q(completed_at__isnull=True) | Q(completed_at__date__gte=previous_start)
        )

        def pct(value, total):
            return round(value * 100 / total, 1) if total else 0.0

        def completion_days(task):
            return max((task.completed_at - task.created_at).total_seconds() / 86400, 0)

        def change(current_value, previous_value, lower_is_better=False):
            if previous_value == 0:
                value = 0.0 if current_value == 0 else 100.0
            else:
                value = round((current_value - previous_value) * 100 / previous_value, 1)
            return {"value": value, "direction": "down" if value < 0 else "up" if value > 0 else "flat",
                    "is_positive": value <= 0 if lower_is_better else value >= 0}

        def assigned_workload(qs, eligible_employee_ids):
            """Return effort shares and assignee count for the selected employees.

            A task's effort is split equally between all of its assignees. Unassigned
            tasks do not contribute to workload, and only eligible active employees
            are included in the returned average.
            """
            eligible_employee_ids = set(eligible_employee_ids)
            workload_by_employee = {}
            for task in qs.prefetch_related("assignees").only("id", "effort_score"):
                assignee_ids = [assignee.id for assignee in task.assignees.all()]
                if not assignee_ids:
                    continue
                effort_share = task.effort_score / len(assignee_ids)
                for assignee_id in assignee_ids:
                    if assignee_id in eligible_employee_ids:
                        workload_by_employee[assignee_id] = (
                            workload_by_employee.get(assignee_id, 0) + effort_share
                        )
            return sum(workload_by_employee.values()), len(workload_by_employee)

        accountable_statuses = (Task.Status.IN_PROGRESS, Task.Status.COMPLETED)

        def snapshot(qs, period_start, as_of):
            # Completion rate = (Completed + Archived) / (Active + Completed + Archived).
            # On Hold, Backlog/Postponed and Not Started are informational only.
            accountable_qs = qs.filter(status__in=accountable_statuses)
            completed_qs = accountable_qs.filter(
                status=Task.Status.COMPLETED,
                completed_at__date__range=(period_start, as_of),
            )
            completed_count = completed_qs.filter(is_archived=False).count()
            archived_count = completed_qs.filter(is_archived=True).count()
            active_count = accountable_qs.filter(status=Task.Status.IN_PROGRESS).count()
            total = active_count + completed_count + archived_count
            overdue_count = qs.filter(due_date__lt=as_of).filter(
                Q(completed_at__isnull=True) | Q(completed_at__date__gt=as_of)
            ).count()
            on_time = completed_qs.filter(Q(due_date__isnull=True) | Q(completed_at__date__lte=F("due_date"))).count()
            durations = [completion_days(task)
                         for task in completed_qs.only("created_at", "completed_at") if task.completed_at]
            return {"total": total, "active": active_count, "completed": completed_count,
                    "archived": archived_count, "done": completed_count + archived_count,
                    "completion_rate": pct(completed_count + archived_count, total),
                    "on_time_rate": pct(on_time, completed_count + archived_count), "overdue": overdue_count,
                    "overdue_rate": pct(overdue_count, total),
                    "avg_completion_days": round(sum(durations) / len(durations), 1) if durations else 0.0}

        current_stats = snapshot(current, start_date, end_date)
        previous_stats = snapshot(previous, previous_start, previous_end)
        active_count = current.filter(Q(completed_at__isnull=True) | Q(completed_at__date__gt=end_date)).count()
        previous_active = previous.filter(Q(completed_at__isnull=True) | Q(completed_at__date__gt=previous_end)).count()

        granularity = params.get("granularity") or ("day" if period_days <= 31 else "week" if period_days <= 120 else "month")
        trunc = {"day": TruncDate, "week": TruncWeek, "month": TruncMonth}.get(granularity)
        if trunc is None:
            return Response({"granularity": "Use day, week or month."}, status=status.HTTP_400_BAD_REQUEST)
        trend_rows = current.annotate(period=trunc("created_at")).values("period").annotate(created=Count("id", distinct=True)).order_by("period")
        completed_rows = universe.filter(completed_at__date__range=(start_date, end_date)).annotate(
            period=trunc("completed_at")).values("period").annotate(completed=Count("id", distinct=True)).order_by("period")
        trend = {}
        for row in trend_rows:
            key = row["period"].date() if isinstance(row["period"], datetime) else row["period"]
            trend.setdefault(key, {"date": key, "created": 0, "completed": 0})["created"] = row["created"]
        for row in completed_rows:
            key = row["period"].date() if isinstance(row["period"], datetime) else row["period"]
            trend.setdefault(key, {"date": key, "created": 0, "completed": 0})["completed"] = row["completed"]

        status_counts = dict(current.filter(is_archived=False).values_list("status").annotate(count=Count("id", distinct=True)))
        archived_count = current.filter(is_archived=True, status=Task.Status.COMPLETED).count()
        status_total = current.count()
        priority_counts = dict(current.values_list("priority").annotate(count=Count("id", distinct=True)))
        status_chart = [{"key": key, "label": label, "count": status_counts.get(key, 0),
                         "percentage": pct(status_counts.get(key, 0), status_total)} for key, label in Task.Status.choices]
        status_chart.append({"key": "archived", "label": "Verified & Archived", "count": archived_count,
                             "percentage": pct(archived_count, status_total)})
        priority_chart = [{"key": key, "label": label, "count": priority_counts.get(key, 0),
                           "percentage": pct(priority_counts.get(key, 0), status_total)} for key, label in Task.Priority.choices]
        no_priority = current.filter(priority="").count()
        priority_chart.append({"key": "no_priority", "label": "No Priority", "count": no_priority,
                               "percentage": pct(no_priority, status_total)})

        department_rows = current.values("department_id", "department__name").annotate(
            total=Count("id", distinct=True),
            total_effort_points=Sum("effort_score"),
            completed=Count("id", filter=Q(completed_at__date__range=(start_date, end_date)), distinct=True),
            in_progress=Count("id", filter=Q(status=Task.Status.IN_PROGRESS), distinct=True),
            on_hold=Count("id", filter=Q(status=Task.Status.ON_HOLD, is_archived=False), distinct=True),
            overdue=Count("id", filter=Q(due_date__lt=end_date) & (Q(completed_at__isnull=True) | Q(completed_at__date__gt=end_date)), distinct=True),
        ).order_by("department__name")
        employee_counts = dict(
            employees.values_list("department_id").annotate(count=Count("id", distinct=True))
        )
        department_performance = []
        for row in department_rows:
            employee_count = employee_counts.get(row["department_id"], 0)
            department_employee_ids = employees.filter(
                department_id=row["department_id"]
            ).values_list("id", flat=True)
            assigned_effort_points, assigned_employee_count = assigned_workload(
                current.filter(department_id=row["department_id"]),
                department_employee_ids,
            )
            department_performance.append({
                "department_id": str(row["department_id"]),
                "department_name": row["department__name"],
                "employees": employee_count,
                "total": row["total"],
                "total_effort_points": row["total_effort_points"] or 0,
                "completed": row["completed"],
                "in_progress": row["in_progress"],
                "on_hold": row["on_hold"],
                "overdue": row["overdue"],
                "tasks_per_employee": round(row["total"] / employee_count, 1) if employee_count else 0.0,
                "completed_tasks_per_employee": round(row["completed"] / employee_count, 1) if employee_count else 0.0,
                "weighted_workload_per_employee": round(assigned_effort_points / assigned_employee_count, 1) if assigned_employee_count else 0.0,
                "workload_assigned_employees": assigned_employee_count,
                "completion_rate": pct(row["completed"], row["total"]),
                "overdue_rate": pct(row["overdue"], row["total"]),
            })

        workload_rows = current.values("assignees__id", "assignees__first_name", "assignees__last_name", "assignees__avatar").exclude(
            assignees__id=None).annotate(active=Count("id", filter=~Q(status=Task.Status.COMPLETED), distinct=True),
            overdue=Count("id", filter=Q(due_date__lt=end_date) & ~Q(status=Task.Status.COMPLETED), distinct=True),
            due_this_week=Count("id", filter=Q(due_date__range=(end_date, end_date + timedelta(days=6))) & ~Q(status=Task.Status.COMPLETED), distinct=True)
        ).order_by("-active", "assignees__first_name")
        workload = [{"employee_id": str(row["assignees__id"]),
                     "full_name": (f'{row["assignees__first_name"]} {row["assignees__last_name"]}').strip(),
                     "avatar": request.build_absolute_uri(settings.MEDIA_URL + row["assignees__avatar"]) if row["assignees__avatar"] else None,
                     "active_tasks": row["active"], "overdue_tasks": row["overdue"], "due_this_week": row["due_this_week"]}
                    for row in workload_rows]

        overdue_qs = current.filter(due_date__lt=end_date).filter(
            Q(completed_at__isnull=True) | Q(completed_at__date__gt=end_date)
        ).select_related(
            "department", "main_assignee").prefetch_related("assignees")
        overdue_items = []
        for task in overdue_qs.order_by("due_date", "-priority", "title"):
            assignee = task.main_assignee or next(iter(task.assignees.all()), None)
            overdue_items.append({"id": str(task.id), "title": task.title, "priority": task.priority,
                                  "status": task.status, "due_date": task.due_date,
                                  "days_overdue": max((end_date - task.due_date).days, 0),
                                  "department": {"id": str(task.department_id), "name": task.department.name},
                                  "assignee": {"id": str(assignee.id), "full_name": assignee.get_full_name() or assignee.email,
                                               "avatar": request.build_absolute_uri(assignee.avatar.url) if assignee and assignee.avatar else None} if assignee else None})

        current_task_list = list(current.prefetch_related("assignees"))
        overdue_trend = []
        cursor = start_date
        step = 1 if granularity == "day" else 7 if granularity == "week" else 30
        while cursor <= end_date:
            overdue_trend.append({"date": cursor, "count": sum(
                task.created_at.date() <= cursor and bool(task.due_date and task.due_date < cursor)
                and (not task.completed_at or task.completed_at.date() > cursor)
                for task in current_task_list
            )})
            cursor += timedelta(days=step)

        def performance_level(score):
            if score is None:
                return "not_rated"
            if score >= 90:
                return "outstanding"
            if score >= 80:
                return "excellent"
            if score >= 70:
                return "good"
            if score >= 60:
                return "needs_improvement"
            return "critical"

        def staff_rows_for(period_tasks, staff, period_start, period_end):
            memberships = []
            for task in period_tasks:
                assignee_ids = {str(user.id) for user in task.assignees.all()}
                if task.main_assignee_id:
                    assignee_ids.add(str(task.main_assignee_id))
                memberships.append((task, assignee_ids))

            rows = []
            for employee in staff:
                employee_tasks = [task for task, ids in memberships if str(employee.id) in ids]
                assigned = len(employee_tasks)
                assigned_effort_points = sum(task.effort_score for task in employee_tasks)
                completed_tasks = [task for task in employee_tasks if task.completed_at and period_start <= task.completed_at.date() <= period_end]
                in_progress = sum(
                    task.status in (Task.Status.IN_PROGRESS, Task.Status.NOT_STARTED)
                    for task in employee_tasks
                )
                on_hold = sum(task.status == Task.Status.ON_HOLD and not task.is_archived for task in employee_tasks)
                overdue = sum(
                    bool(task.due_date and task.due_date < period_end and (not task.completed_at or task.completed_at.date() > period_end))
                    for task in employee_tasks
                )
                durations = [completion_days(task) for task in completed_tasks]

                eligible_tasks = [
                    task for task in employee_tasks
                    if task.status in accountable_statuses
                    and task.due_date and period_start <= task.due_date <= period_end
                ]
                eligible_completed = [
                    task for task in eligible_tasks
                    if task.completed_at and task.completed_at.date() <= period_end
                ]
                eligible_on_time = sum(task.completed_at.date() <= task.due_date for task in eligible_completed)
                eligible_overdue = sum(
                    task.due_date < period_end and (not task.completed_at or task.completed_at.date() > period_end)
                    for task in eligible_tasks
                )
                completion_rate = pct(len(eligible_completed), len(eligible_tasks))
                on_time_rate = pct(eligible_on_time, len(eligible_completed))
                overdue_control = 100.0 - pct(eligible_overdue, len(eligible_tasks)) if eligible_tasks else 0.0
                score = round(
                    completion_rate * .45 + on_time_rate * .35 + overdue_control * .20
                ) if eligible_tasks else None
                rows.append({
                    "employee": {"id": str(employee.id), "full_name": employee.get_full_name() or employee.email,
                                 "email": employee.email, "avatar": request.build_absolute_uri(employee.avatar.url) if employee.avatar else None,
                                 "job_title": employee.job_title},
                    "department": {"id": str(employee.department_id), "name": employee.department.name} if employee.department else None,
                    "assigned_tasks": assigned, "assigned_effort_points": assigned_effort_points,
                    "completed_tasks": len(completed_tasks), "in_progress_tasks": in_progress,
                    "on_hold_tasks": on_hold,
                    "overdue_tasks": overdue, "on_time_rate": on_time_rate,
                    "avg_completion_days": round(sum(durations) / len(durations), 1) if durations else 0.0,
                    "completion_rate": completion_rate, "overdue_control": overdue_control,
                    "performance_eligible_tasks": len(eligible_tasks),
                    "performance_completed_tasks": len(eligible_completed),
                    "performance_overdue_tasks": eligible_overdue,
                    "performance_score": score,
                    "performance_level": performance_level(score),
                })
            return rows

        staff = list(employees.select_related("department"))
        previous_task_list = list(previous.prefetch_related("assignees"))
        all_staff_rows = staff_rows_for(current_task_list, staff, start_date, end_date)
        previous_staff_rows = staff_rows_for(previous_task_list, staff, previous_start, previous_end)

        level_filter = params.get("performance_level")
        performance_levels = ("outstanding", "excellent", "good", "needs_improvement", "critical", "not_rated")
        if level_filter and level_filter not in performance_levels:
            return Response({"performance_level": "Invalid performance level."}, status=status.HTTP_400_BAD_REQUEST)
        staff_filter = params.get("staff_filter", "all")
        if staff_filter not in ("all", "top_performers", "needs_attention"):
            return Response({"staff_filter": "Use all, top_performers or needs_attention."}, status=status.HTTP_400_BAD_REQUEST)
        filtered_staff_rows = all_staff_rows
        staff_search = params.get("staff_search", "").strip().casefold()
        if staff_search:
            filtered_staff_rows = [row for row in filtered_staff_rows if staff_search in " ".join((
                row["employee"]["full_name"], row["employee"]["email"], row["employee"]["job_title"],
                row["department"]["name"] if row["department"] else "",
            )).casefold()]
        if level_filter:
            filtered_staff_rows = [row for row in filtered_staff_rows if row["performance_level"] == level_filter]
        if staff_filter == "top_performers":
            filtered_staff_rows = [row for row in filtered_staff_rows if row["performance_level"] in ("outstanding", "excellent")]
        elif staff_filter == "needs_attention":
            filtered_staff_rows = [row for row in filtered_staff_rows if row["performance_level"] in ("needs_improvement", "critical")]

        # Staff performance is always ranked by assigned task volume, highest first.
        # This intentionally ignores legacy `ordering=-performance` values still
        # sent by older frontend builds.
        ordering = "-assigned"
        filtered_staff_rows.sort(
            key=lambda row: (-row["assigned_tasks"], row["employee"]["full_name"].casefold())
        )
        try:
            page = int(params.get("page", 1))
            page_size = int(params.get("page_size", 8))
            if page < 1 or page_size < 1 or page_size > 100:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"pagination": "page must be >= 1 and page_size must be between 1 and 100."}, status=status.HTTP_400_BAD_REQUEST)
        staff_count = len(filtered_staff_rows)
        page_count = (staff_count + page_size - 1) // page_size
        offset = (page - 1) * page_size
        paged_staff_rows = filtered_staff_rows[offset:offset + page_size]

        scored = [row["performance_score"] for row in all_staff_rows if row["performance_score"] is not None]
        previous_scored = [row["performance_score"] for row in previous_staff_rows if row["performance_score"] is not None]
        average_performance = round(sum(scored) / len(scored), 1) if scored else 0.0
        previous_average_performance = round(sum(previous_scored) / len(previous_scored), 1) if previous_scored else 0.0
        current_staff_count = len(staff)
        previous_staff_count = sum(employee.date_joined.date() <= previous_end for employee in staff)
        assigned_effort_points, assigned_employee_count = assigned_workload(
            current,
            employees.values_list("id", flat=True),
        )
        workload_kpis = {
            "tasks_per_employee": {
                "value": round(status_total / current_staff_count, 1) if current_staff_count else 0.0,
                "unit": "tasks_per_employee",
                "total_tasks": status_total,
                "employees": current_staff_count,
            },
            "completed_tasks_per_employee": {
                "value": round(current_stats["done"] / current_staff_count, 1) if current_staff_count else 0.0,
                "unit": "tasks_per_employee",
                "completed_tasks": current_stats["done"],
                "employees": current_staff_count,
            },
            "completion_rate": {
                "value": pct(current_stats["done"], status_total),
                "unit": "percent",
                "completed_tasks": current_stats["done"],
                "total_tasks": status_total,
            },
            "overdue_rate": {
                "value": pct(current_stats["overdue"], status_total),
                "unit": "percent",
                "overdue_tasks": current_stats["overdue"],
                "total_tasks": status_total,
            },
            "weighted_workload_per_employee": {
                "value": round(assigned_effort_points / assigned_employee_count, 1) if assigned_employee_count else 0.0,
                "unit": "effort_points_per_employee",
                "total_effort_points": round(assigned_effort_points, 1),
                "employees": assigned_employee_count,
            },
        }
        analytics_cards = [
            {"key": "total_average_performance", "label": "Total Average Performance",
             "value": average_performance, "unit": "percent"},
            {"key": "total_average_on_time", "label": "Total Average On-time",
             "value": current_stats["on_time_rate"], "unit": "percent"},
            {"key": "total_average_completion_days", "label": "Total Average Time",
             "value": current_stats["avg_completion_days"], "unit": "days"},
            {"key": "tasks_per_employee", "label": "Tasks per Employee",
             **workload_kpis["tasks_per_employee"]},
            {"key": "overdue_rate", "label": "Overdue Rate",
             **workload_kpis["overdue_rate"]},
            {"key": "weighted_workload_per_employee", "label": "Weighted Workload per Employee",
             **workload_kpis["weighted_workload_per_employee"]},
        ]

        # This chart now compares task activity instead of a derived score.
        # There is no assignment-history timestamp yet, so created_at is used as
        # the initial assignment date.
        performance_trend = [
            {"date": item["date"], "assigned": item["created"], "completed": item["completed"]}
            for item in sorted(trend.values(), key=lambda item: item["date"])
        ]

        response = {
            "meta": {"start_date": start_date, "end_date": end_date, "previous_start_date": previous_start,
                     "previous_end_date": previous_end, "granularity": granularity, "generated_at": timezone.now(),
                     "applied_filters": {"department": department_id, "employee": employee_id, "priority": priority,
                                         "status": task_status, "search": search or None,
                                         "staff_search": staff_search or None,
                                         "performance_level": level_filter, "staff_filter": staff_filter,
                                         "ordering": ordering, "page": page, "page_size": page_size},
                     "filter_options": {"departments": [{"id": str(d.id), "name": d.name} for d in departments],
                                        "employees": [{"id": str(u.id), "full_name": u.get_full_name() or u.email} for u in employee_options],
                                        "priorities": [{"value": k, "label": v} for k, v in Task.Priority.choices],
                                        "statuses": [{"value": k, "label": v} for k, v in Task.Status.choices],
                                        "performance_levels": [{"value": "outstanding", "label": "Outstanding"},
                                                               {"value": "excellent", "label": "Excellent"},
                                                               {"value": "good", "label": "Good"},
                                                               {"value": "needs_improvement", "label": "Needs Improvement"},
                                                               {"value": "critical", "label": "Critical"},
                                                               {"value": "not_rated", "label": "Not Rated"}]}},
            "summary": {
                "cards": analytics_cards,
                "workload_kpis": workload_kpis,
                "total_staff": {"value": current_staff_count, "unit": "staff", "change": change(current_staff_count, previous_staff_count)},
                "average_performance": {"value": average_performance, "unit": "percent", "change": change(average_performance, previous_average_performance)},
                "total_average_performance": {"value": average_performance, "unit": "percent", "change": change(average_performance, previous_average_performance)},
                "total_average_on_time": {"value": current_stats["on_time_rate"], "unit": "percent", "change": change(current_stats["on_time_rate"], previous_stats["on_time_rate"])},
                "total_average_completion_days": {"value": current_stats["avg_completion_days"], "unit": "days", "change": change(current_stats["avg_completion_days"], previous_stats["avg_completion_days"], True)},
                "task_completion_rate": {"value": current_stats["completion_rate"], "unit": "percent", "change": change(current_stats["completion_rate"], previous_stats["completion_rate"])},
                "active_tasks": {"value": current_stats["active"], "unit": "tasks"},
                "completed_tasks": {"value": current_stats["completed"], "unit": "tasks"},
                "archived_tasks": {"value": current_stats["archived"], "unit": "tasks", "label": "Verified & Archived"},
                "on_hold_tasks": {"value": current.filter(is_archived=False, status=Task.Status.ON_HOLD).count(), "unit": "tasks"},
                "postponed_tasks": {"value": current.filter(is_archived=False, status=Task.Status.BACKLOG).count(), "unit": "tasks"},
                "on_time_completion": {"value": current_stats["on_time_rate"], "unit": "percent", "change": change(current_stats["on_time_rate"], previous_stats["on_time_rate"])},
                "overdue_rate": {"value": current_stats["overdue_rate"], "unit": "percent", "count": current_stats["overdue"], "change": change(current_stats["overdue_rate"], previous_stats["overdue_rate"], True)},
                "avg_completion_time": {"value": current_stats["avg_completion_days"], "unit": "days", "change": change(current_stats["avg_completion_days"], previous_stats["avg_completion_days"], True)},
                "active_workload": {"value": active_count, "unit": "tasks", "staff_count": len(workload), "change": change(active_count, previous_active)},
            },
            "charts": {"task_completion_trend": sorted(trend.values(), key=lambda item: item["date"]),
                       "performance_trend": performance_trend,
                       "performance_trend_series": [
                           {"key": "completed", "label": "Completed", "color": "#34D399"},
                           {"key": "assigned", "label": "Assigned", "color": "#EF4444"},
                       ],
                       "task_status": {"total": status_total, "items": status_chart},
                       "department_performance": department_performance, "team_workload": workload,
                       "department_workload_intensity": department_performance,
                       "workload_distribution": department_performance,
                       "tasks_by_priority": {"total": status_total, "items": priority_chart},
                       "overdue_trend": overdue_trend},
            "staff_performance": {"count": staff_count, "page": page, "page_size": page_size,
                                  "total_pages": page_count, "next_page": page + 1 if page < page_count else None,
                                  "previous_page": page - 1 if page > 1 and page_count else None,
                                  "results": paged_staff_rows},
            "overdue": {"count": len(overdue_items), "staff_count": len({item["assignee"]["id"] for item in overdue_items if item["assignee"]}), "items": overdue_items},
            # Legacy fields retained so existing frontend clients do not break.
            "task_completion_rate": current_stats["completion_rate"],
            "team_velocity": universe.filter(completed_at__date__range=(start_date, end_date)).count(),
            "overdue_tasks": current_stats["overdue"],
            "monthly_progress": sorted(trend.values(), key=lambda item: item["date"]),
            "tasks_by_category": list(current.values("category").annotate(count=Count("id", distinct=True)).order_by("-count")),
        }
        return Response(response)


class DashboardView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        responses=inline_serializer(
            name="DashboardResponse",
            fields={
                "summary": serializers.JSONField(),
                "today_events": serializers.JSONField(),
                "upcoming_events": serializers.JSONField(),
                "upcoming_deadlines": serializers.JSONField(),
                "tasks_by_department": serializers.JSONField(),
                "recent_tasks": serializers.JSONField(),
            },
        )
    )
    def get(self, request):
        user = request.user
        # Owners see organization-wide data. Every other role is restricted to
        # explicitly assigned departments; members additionally keep their
        # assignee-based task visibility below.
        metric_tasks = Task.objects.all()
        all_tasks = Task.objects.select_related("department", "created_by").prefetch_related("assignees")
        events = Event.objects.select_related("department", "created_by").prefetch_related("attendees")

        has_organization_access = user.is_superuser or user.role == User.Role.OWNER
        if not has_organization_access:
            access_filter = Q(department_id=user.department_id) | Q(department__users_with_access=user)
            metric_tasks = metric_tasks.filter(access_filter)
            all_tasks = all_tasks.filter(access_filter)
            events = events.filter(access_filter)

        if user.role not in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER):
            assigned_task_ids = Task.objects.filter(assignees=user).values("pk")
            member_task_filter = Q(department=user.department) | Q(pk__in=assigned_task_ids)
            all_tasks = Task.objects.filter(member_task_filter).select_related(
                "department", "created_by"
            ).prefetch_related("assignees")
            metric_tasks = metric_tasks.filter(member_task_filter)
            events = Event.objects.filter(
                Q(department=user.department) | Q(attendees=user)
            ).select_related("department", "created_by").prefetch_related("attendees")

        all_tasks = visible_tasks_for(all_tasks, user).distinct()
        metric_tasks = visible_tasks_for(metric_tasks, user).distinct()
        tasks = all_tasks.filter(is_archived=False)
        events = events.distinct()
        today = timezone.localdate()
        now = timezone.now()
        day_start = timezone.make_aware(datetime.combine(today, time.min))
        day_end = day_start + timedelta(days=1)

        summary = metric_tasks.aggregate(
            total=Count("id", distinct=True),
            archived=Count("id", filter=Q(is_archived=True), distinct=True),
            completed=Count("id", filter=Q(is_archived=False, status=Task.Status.COMPLETED), distinct=True),
            in_progress=Count("id", filter=Q(is_archived=False, status=Task.Status.IN_PROGRESS), distinct=True),
            not_started=Count("id", filter=Q(is_archived=False, status=Task.Status.NOT_STARTED), distinct=True),
            backlog=Count("id", filter=Q(is_archived=False, status=Task.Status.BACKLOG), distinct=True),
            on_hold=Count("id", filter=Q(is_archived=False, status=Task.Status.ON_HOLD), distinct=True),
            overdue=Count(
                "id",
                filter=Q(is_archived=False, due_date__lt=today) & ~Q(status=Task.Status.COMPLETED),
                distinct=True,
            ),
        )
        total = summary["total"]
        archived = summary["archived"]
        completed = summary["completed"]
        in_progress = summary["in_progress"]
        not_started = summary["not_started"]
        backlog = summary["backlog"]
        on_hold = summary["on_hold"]
        overdue = summary["overdue"]

        def percent(value):
            return round(value * 100 / total, 1) if total else 0.0

        def event_data(event):
            attendees = list(event.attendees.all())
            return {
                "id": str(event.id),
                "title": event.title,
                "event_type": event.event_type,
                "department": {"id": str(event.department_id), "name": event.department.name} if event.department else None,
                "starts_at": event.starts_at,
                "ends_at": event.ends_at,
                "location": event.location,
                "meeting_url": event.meeting_url,
                "attendee_count": len(attendees),
                "attendees": UserBriefSerializer(attendees[:3], many=True, context={"request": request}).data,
            }

        today_events = [event_data(event) for event in events.filter(starts_at__gte=day_start, starts_at__lt=day_end).order_by("starts_at")[:4]]
        upcoming_events = [event_data(event) for event in events.filter(starts_at__gte=day_end).order_by("starts_at")[:4]]

        deadline_items = []
        for task in tasks.exclude(status=Task.Status.COMPLETED).filter(due_date__gte=today).order_by("due_date", "created_at")[:4]:
            deadline_items.append({
                "id": str(task.id),
                "title": task.title,
                "department": {"id": str(task.department_id), "name": task.department.name},
                "priority": task.priority,
                "status": task.status,
                "due_date": task.due_date,
                "days_remaining": (task.due_date - today).days,
            })

        department_items = list(
            metric_tasks.filter(is_archived=False).values("department_id", "department__name")
            .annotate(task_count=Count("id", distinct=True))
            .order_by("-task_count", "department__name")
        )
        department_total = sum(item["task_count"] for item in department_items)

        def department_percent(value):
            return round(value * 100 / department_total, 1) if department_total else 0.0

        tasks_by_department = [
            {
                "department_id": str(item["department_id"]),
                "department_name": item["department__name"],
                "task_count": item["task_count"],
                "percentage": department_percent(item["task_count"]),
            }
            for item in department_items
        ]

        recent_tasks = [
            {
                "id": str(task.id),
                "title": task.title,
                "department": {"id": str(task.department_id), "name": task.department.name},
                "status": task.status,
                "priority": task.priority,
                "created_by_detail": TaskCreatorSerializer(
                    task.created_by,
                    context={"request": request},
                ).data,
                "created_at": task.created_at,
            }
            for task in tasks.order_by("-created_at")[:5]
        ]

        return Response({
            "summary": {
                "total_tasks": {"count": total, "percentage": 100.0 if total else 0.0},
                "archived_tasks": {"count": archived, "percentage": percent(archived)},
                "completed_tasks": {"count": completed, "percentage": percent(completed)},
                "in_progress_tasks": {"count": in_progress, "percentage": percent(in_progress)},
                "not_started_tasks": {"count": not_started, "percentage": percent(not_started)},
                "backlog_tasks": {"count": backlog, "percentage": percent(backlog)},
                "on_hold_tasks": {"count": on_hold, "percentage": percent(on_hold)},
                "overdue_tasks": {"count": overdue, "percentage": percent(overdue)},
            },
            "today_events": today_events,
            "upcoming_events": upcoming_events,
            "upcoming_deadlines": deadline_items,
            "tasks_by_department": tasks_by_department,
            "recent_tasks": recent_tasks,
            "generated_at": now,
        })


class SupportBotView(APIView):
    permission_classes = (IsAuthenticated,)
    parser_classes = (JSONParser, FormParser, MultiPartParser)

    @extend_schema(
        request=SupportBotMessageSerializer,
        responses={200: inline_serializer(
            name="SupportBotResponse",
            fields={"detail": serializers.CharField()},
        )},
    )
    def post(self, request):
        serializer = SupportBotMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token = settings.TELEGRAM_SUPPORT_BOT_TOKEN
        chat_id = settings.TELEGRAM_SUPPORT_CHAT_ID
        if not token or not chat_id:
            return Response(
                {"detail": "Support bot is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user = request.user
        sender_name = user.get_full_name() or user.username or user.email
        department = user.department.name if user.department else "No department"
        text = (
            "TaskFlow support request\n"
            f"From: {sender_name}\n"
            f"Email: {user.email}\n"
            f"Department: {department}\n\n"
            f"{serializer.validated_data['message']}"
        )
        try:
            send_support_message(
                token=token,
                chat_id=chat_id,
                text=text,
                screenshot=serializer.validated_data.get("screenshot"),
            )
        except TelegramSupportError:
            return Response(
                {"detail": "Could not send the support request. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": "Support request sent successfully."})


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer
    permission_classes = (IsAuthenticated,)

    def get_object(self):
        return self.request.user


class PreferenceView(generics.RetrieveUpdateAPIView):
    serializer_class = UserPreferenceSerializer
    permission_classes = (IsAuthenticated,)

    def get_object(self):
        preference, _ = UserPreference.objects.get_or_create(user=self.request.user)
        return preference


class PasswordChangeView(generics.GenericAPIView):
    serializer_class = PasswordChangeSerializer
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response({"detail": "Password updated successfully."})


class TwoFactorView(generics.GenericAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = TwoFactorSerializer

    def patch(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.two_factor_enabled = serializer.validated_data["enabled"]
        request.user.save(update_fields=["two_factor_enabled"])
        return Response({"two_factor_enabled": request.user.two_factor_enabled})


class AccountDeleteView(generics.GenericAPIView):
    serializer_class = AccountDeleteSerializer
    permission_classes = (IsAuthenticated,)

    @transaction.atomic
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.is_active = False
        user.email = f"deleted-{user.pk}@deleted.local"
        user.set_unusable_password()
        user.save(update_fields=["is_active", "email", "password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConversationViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Conversation.objects.none()
    serializer_class = ConversationSerializer
    filter_backends = (filters.SearchFilter, filters.OrderingFilter)
    search_fields = ("title", "participants__first_name", "participants__last_name", "participants__email")
    ordering_fields = ("updated_at", "created_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Conversation.objects.filter(participants=self.request.user).select_related("department").prefetch_related(
            "participants",
            Prefetch(
                "messages",
                queryset=Message.objects.filter(is_deleted=False).select_related("sender"),
                to_attr="visible_messages",
            ),
            Prefetch(
                "participant_links",
                queryset=ConversationParticipant.objects.filter(user=self.request.user),
                to_attr="current_user_links",
            ),
        ).distinct()
        return qs.filter(department_id=self.department_id()) if self.department_id() else qs

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        department = serializer.validated_data["department"]
        self.ensure_member(department)

        participants = set(serializer.validated_data.get("participants", [])) | {request.user}
        if not serializer.validated_data.get("is_group", False):
            # Lock both users so simultaneous requests cannot create two direct
            # conversations for the same pair.
            participant_ids = sorted((user.pk for user in participants), key=str)
            list(User.objects.select_for_update().filter(pk__in=participant_ids).order_by("pk"))
            existing = (
                Conversation.objects.filter(department=department, is_group=False)
                .annotate(
                    participant_count=Count("participants", distinct=True),
                    matched_participant_count=Count(
                        "participants",
                        filter=Q(participants__pk__in=participant_ids),
                        distinct=True,
                    ),
                )
                .filter(participant_count=2, matched_participant_count=2)
                .first()
            )
            if existing:
                existing = self.get_queryset().filter(pk=existing.pk).first() or existing
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @transaction.atomic
    def perform_create(self, serializer):
        department = serializer.validated_data["department"]
        self.ensure_member(department)
        conversation = serializer.save()
        participants = set(serializer.validated_data.get("participants", [])) | {self.request.user}
        for user in participants:
            ConversationParticipant.objects.get_or_create(conversation=conversation, user=user)

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        link = self.get_object().participant_links.get(user=request.user)
        link.last_read_at = timezone.now()
        link.save(update_fields=["last_read_at", "updated_at"])
        return Response({"detail": "Conversation marked as read."})


class MessageViewSet(viewsets.ModelViewSet):
    queryset = Message.objects.none()
    serializer_class = MessageSerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPagination
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_fields = ("conversation",)
    ordering_fields = ("created_at",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return Message.objects.filter(conversation__participants=self.request.user, is_deleted=False).select_related("sender", "conversation").distinct()

    def perform_create(self, serializer):
        conversation = serializer.validated_data["conversation"]
        if not conversation.participants.filter(pk=self.request.user.pk).exists():
            raise serializers.ValidationError({"conversation": "You are not a participant."})
        message = create_message(
            conversation=conversation,
            sender=self.request.user,
            body=serializer.validated_data.get("body", ""),
            attachment=serializer.validated_data.get("attachment"),
        )
        serializer.instance = message

    def perform_destroy(self, instance):
        if instance.sender != self.request.user:
            raise serializers.ValidationError("Only the sender can delete this message.")
        instance.is_deleted = True
        instance.body = ""
        instance.attachment.delete(save=False)
        instance.save(update_fields=["is_deleted", "body", "attachment", "updated_at"])


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Notification.objects.none()
    serializer_class = NotificationSerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPagination
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_fields = ("notification_type",)
    ordering_fields = ("created_at",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Notification.objects.filter(recipient=self.request.user).select_related("actor", "task", "message")
        unread = self.request.query_params.get("unread", "").lower()
        return qs.filter(read_at__isnull=True) if unread in ("1", "true", "yes") else qs

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        return Response({"unread_count": self.get_queryset().filter(read_at__isnull=True).count()})

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at", "updated_at"])
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        updated = self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"updated": updated})


class TelegramIntegrationView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        integration = TelegramIntegration.objects.filter(user=request.user).first()
        return Response({
            "is_connected": bool(integration and integration.is_connected),
            "telegram_username": integration.telegram_username if integration else "",
            "notifications_enabled": integration.notifications_enabled if integration else True,
            "connected_at": integration.connected_at if integration else None,
        })

    def post(self, request):
        if not settings.TELEGRAM_BOT_USERNAME:
            return Response(
                {"detail": "Telegram bot is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        integration, _ = TelegramIntegration.objects.get_or_create(user=request.user)
        integration.link_token = secrets.token_urlsafe(32)
        integration.link_token_expires_at = timezone.now() + timedelta(minutes=15)
        integration.save(update_fields=["link_token", "link_token_expires_at", "updated_at"])
        return Response({
            "connect_url": f"https://t.me/{settings.TELEGRAM_BOT_USERNAME.lstrip('@')}?start={integration.link_token}",
            "expires_at": integration.link_token_expires_at,
        })

    def patch(self, request):
        integration = TelegramIntegration.objects.filter(user=request.user).first()
        if not integration:
            return Response({"detail": "Telegram is not connected."}, status=status.HTTP_404_NOT_FOUND)
        if "notifications_enabled" not in request.data:
            return Response({"notifications_enabled": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)
        value = request.data["notifications_enabled"]
        if not isinstance(value, bool):
            return Response({"notifications_enabled": "Must be a boolean."}, status=status.HTTP_400_BAD_REQUEST)
        integration.notifications_enabled = value
        integration.save(update_fields=["notifications_enabled", "updated_at"])
        return Response({"notifications_enabled": integration.notifications_enabled})

    def delete(self, request):
        integration = TelegramIntegration.objects.filter(user=request.user).first()
        if integration:
            integration.telegram_user_id = None
            integration.telegram_chat_id = None
            integration.telegram_username = ""
            integration.link_token = None
            integration.link_token_expires_at = None
            integration.is_connected = False
            integration.connected_at = None
            integration.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TelegramWebhookView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        expected = settings.TELEGRAM_WEBHOOK_SECRET
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            return Response(status=status.HTTP_403_FORBIDDEN)

        message = request.data.get("message") or {}
        text = message.get("text", "")
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return Response({"ok": True})

        if text.startswith("/start "):
            token = text.split(maxsplit=1)[1].strip()
            integration = TelegramIntegration.objects.filter(
                link_token=token,
                link_token_expires_at__gt=timezone.now(),
            ).first()
            if not integration:
                bot_api("sendMessage", chat_id=chat_id, text="This connection link is invalid or expired.")
                return Response({"ok": True})
            conflict = TelegramIntegration.objects.filter(telegram_user_id=sender.get("id")).exclude(pk=integration.pk)
            if conflict.exists():
                bot_api("sendMessage", chat_id=chat_id, text="This Telegram account is already connected.")
                return Response({"ok": True})
            integration.telegram_user_id = sender.get("id")
            integration.telegram_chat_id = chat_id
            integration.telegram_username = sender.get("username", "")
            integration.is_connected = True
            integration.connected_at = timezone.now()
            integration.link_token = None
            integration.link_token_expires_at = None
            integration.save()
            bot_api("sendMessage", chat_id=chat_id, text="✅ Telegram successfully connected to TaskFlow.")
        elif text.startswith("/start"):
            bot_api("sendMessage", chat_id=chat_id, text="Open TaskFlow → Profile → Connect Telegram first.")
        return Response({"ok": True})


class TelegramWebhookSetupView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a superuser can configure the Telegram webhook.")
        if not settings.TELEGRAM_WEBHOOK_SECRET:
            return Response({"detail": "TELEGRAM_WEBHOOK_SECRET is not configured."}, status=503)
        try:
            result = bot_api(
                "setWebhook",
                url=webhook_url(request),
                secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
                allowed_updates='["message"]',
            )
        except TelegramError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"ok": bool(result), "url": webhook_url(request)})


class ReportViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Report.objects.none()
    serializer_class = ReportSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_fields = ("report_type", "status")
    search_fields = ("name",)
    ordering_fields = ("created_at", "name", "status")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Report.objects.select_related("generated_by", "department").distinct()
        qs = self.scope_departments(qs)
        return qs.filter(department_id=self.department_id()) if self.department_id() else qs

    def perform_create(self, serializer):
        department = serializer.validated_data["department"]
        self.ensure_member(department)
        report = serializer.save(generated_by=self.request.user)
        tasks = visible_tasks_for(Task.objects.filter(department=department), self.request.user)
        result = {"tasks": tasks.count(), "completed": tasks.filter(status=Task.Status.COMPLETED).count(), "in_progress": tasks.filter(status=Task.Status.IN_PROGRESS).count(), "members": department.users.filter(is_active=True).count()}
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric", "value"])
        writer.writerows(result.items())
        report.result = result
        report.status = Report.Status.READY
        report.file.save(f"{report.id}.csv", ContentFile(buffer.getvalue().encode("utf-8")), save=False)
        report.save(update_fields=["result", "status", "file", "updated_at"])

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        report = self.get_object()
        if not report.file:
            return Response({"detail": "Report file is not ready."}, status=status.HTTP_409_CONFLICT)
        return FileResponse(report.file.open("rb"), as_attachment=True, filename=f"{report.name}.csv")
