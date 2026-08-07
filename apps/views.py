import csv
import io
from datetime import datetime, time, timedelta

from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.http import FileResponse
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.db.models.functions import TruncMonth
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
from drf_spectacular.utils import extend_schema, inline_serializer

from .filters import EventFilter, TaskFilter
from .models import Conversation, ConversationParticipant, Department, Event, Message, Notification, Report, Task, User, UserPreference
from .notifications import notify_new_message, notify_task_assigned, notify_task_completed
from .pagination import StandardPagination
from .permissions import IsDepartmentMember
from .serializers import AccountDeleteSerializer, ConversationSerializer, DepartmentSerializer, EventSerializer, MemberSerializer, MessageSerializer, NotificationSerializer, PasswordChangeSerializer, PasswordResetConfirmSerializer, PasswordResetRequestSerializer, ProfileSerializer, ReportSerializer, SupportBotMessageSerializer, TaskCreatorSerializer, TaskSerializer, TwoFactorSerializer, UserBriefSerializer, UserPreferenceSerializer
from .telegram_support import TelegramSupportError, send_support_message
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
            send_mail(
                subject="TaskFlow parolini tiklash",
                message=(
                    f"Salom {user.get_full_name() or user.email},\n\n"
                    "TaskFlow parolingizni yangilash uchun quyidagi havolani oching:\n"
                    f"{reset_url}\n\n"
                    "Agar bu so‘rovni siz yubormagan bo‘lsangiz, xabarni e’tiborsiz qoldiring."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
            )

        return Response({
            "detail": "Agar ushbu email mavjud bo‘lsa, parolni tiklash havolasi yuborildi."
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
    search_fields = ("user__first_name", "user__last_name", "user__email", "user__job_title")
    ordering_fields = ("joined_at", "user__first_name")

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


class TaskViewSet(DepartmentScopedMixin, viewsets.ModelViewSet):
    queryset = Task.objects.none()
    serializer_class = TaskSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_class = TaskFilter
    search_fields = ("title", "description", "department__name")
    ordering_fields = ("title", "due_date", "priority", "status", "created_at", "progress")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        user = self.request.user
        if user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER):
            qs = Task.objects.all()
        else:
            qs = Task.objects.filter(department=user.department, assignees=user)
        qs = self.scope_departments(qs)
        qs = visible_tasks_for(qs, user)
        qs = qs.select_related("department", "created_by", "main_assignee").prefetch_related("assignees").distinct()
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
        department = serializer.validated_data["department"]
        self.ensure_department_task_manager(department)
        task = serializer.save(created_by=self.request.user)
        notify_task_assigned(task, self.request.user, task.assignees.all())

    def ensure_department_task_manager(self, department):
        user = self.request.user
        can_manage = (
            user.is_active
            and user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)
            and user.can_access_department(department)
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
        if not user.is_active or not user.can_access_department(target_department):
            raise PermissionDenied("You are not a member of this department.")

        is_manager = user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)
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
        responses=inline_serializer(
            name="AnalyticsResponse",
            fields={
                "task_completion_rate": serializers.FloatField(),
                "team_velocity": serializers.IntegerField(),
                "overdue_tasks": serializers.IntegerField(),
                "monthly_progress": serializers.ListField(),
                "tasks_by_category": serializers.ListField(),
            },
        )
    )
    def get(self, request):
        department_id = request.query_params.get("department")
        if not department_id:
            return Response({"department": "This query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.can_access_department(department_id):
            return Response({"detail": "You are not a member of this department."}, status=status.HTTP_403_FORBIDDEN)
        tasks = visible_tasks_for(Task.objects.filter(department_id=department_id), request.user)
        total = tasks.count()
        completed = tasks.filter(status=Task.Status.COMPLETED)
        completion_rate = round(completed.count() * 100 / total, 1) if total else 0
        monthly = list(tasks.annotate(month=TruncMonth("created_at")).values("month").annotate(created=Count("id"), completed=Count("id", filter=Q(status=Task.Status.COMPLETED))).order_by("month"))
        categories = list(tasks.values("category").annotate(count=Count("id")).order_by("-count"))
        overdue = tasks.exclude(status=Task.Status.COMPLETED).filter(due_date__lt=timezone.localdate()).count()
        velocity = completed.filter(completed_at__gte=timezone.now() - timedelta(days=7)).count()
        return Response({"task_completion_rate": completion_rate, "team_velocity": velocity, "overdue_tasks": overdue, "monthly_progress": monthly, "tasks_by_category": categories})


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
        tasks = Task.objects.filter(is_archived=False).select_related("department", "created_by").prefetch_related("assignees")
        events = Event.objects.select_related("department", "created_by").prefetch_related("attendees")

        if not user.is_superuser and not user.has_all_departments_access:
            access_filter = Q(department_id=user.department_id) | Q(department__users_with_access=user)
            tasks = tasks.filter(access_filter)
            events = events.filter(access_filter)

        if user.role not in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER):
            tasks = tasks.filter(department=user.department)
            events = events.filter(department=user.department)
            tasks = tasks.filter(assignees=user)
            events = events.filter(attendees=user)

        tasks = visible_tasks_for(tasks, user).distinct()
        events = events.distinct()
        today = timezone.localdate()
        now = timezone.now()
        day_start = timezone.make_aware(datetime.combine(today, time.min))
        day_end = day_start + timedelta(days=1)

        summary = tasks.aggregate(
            total=Count("id"),
            completed=Count("id", filter=Q(status=Task.Status.COMPLETED)),
            in_progress=Count("id", filter=Q(status=Task.Status.IN_PROGRESS)),
            not_started=Count("id", filter=Q(status=Task.Status.NOT_STARTED)),
            backlog=Count("id", filter=Q(status=Task.Status.BACKLOG)),
            on_hold=Count("id", filter=Q(status=Task.Status.ON_HOLD)),
            overdue=Count(
                "id",
                filter=Q(due_date__lt=today) & ~Q(status=Task.Status.COMPLETED),
            ),
        )
        total = summary["total"]
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
            tasks.values("department_id", "department__name")
            .annotate(task_count=Count("id"))
            .order_by("-task_count", "department__name")
        )
        tasks_by_department = [
            {
                "department_id": str(item["department_id"]),
                "department_name": item["department__name"],
                "task_count": item["task_count"],
                "percentage": percent(item["task_count"]),
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
        message = serializer.save(sender=self.request.user)
        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
        notify_new_message(message)

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
