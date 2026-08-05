import csv
import io
from datetime import timedelta

from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.http import FileResponse
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, inline_serializer

from .filters import EventFilter, TaskFilter
from .models import Conversation, ConversationParticipant, Department, Event, Message, Report, Task, User, UserPreference
from .pagination import StandardPagination
from .permissions import IsDepartmentMember
from .serializers import AccountDeleteSerializer, ConversationSerializer, DepartmentSerializer, EventSerializer, MemberSerializer, MessageSerializer, PasswordChangeSerializer, PasswordResetConfirmSerializer, PasswordResetRequestSerializer, ProfileSerializer, ReportSerializer, TaskSerializer, TwoFactorSerializer, UserPreferenceSerializer


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
        if not user.is_active or not (user.role in (User.Role.OWNER, User.Role.ADMIN) or user.department_id == department.id):
            raise serializers.ValidationError({"department": "You are not a member of this department."})


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
            and (
                current.role in (User.Role.OWNER, User.Role.ADMIN)
                or (department is not None and current.role == User.Role.MANAGER and current.department_id == department.id)
            )
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = User.objects.select_related("department")
        if not self.request.user.is_superuser:
            qs = qs if self.request.user.role in self.manager_roles[:2] else qs.filter(department=self.request.user.department)
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
        if self.request.user.role == User.Role.MANAGER and serializer.validated_data.get("role", serializer.instance.role) != User.Role.MEMBER:
            raise PermissionDenied("Managers can only manage regular members.")
        serializer.save()

    def perform_destroy(self, instance):
        if not self.can_manage(instance.department):
            raise PermissionDenied("Only an Owner, Admin, or Manager of this department can remove members.")
        instance.delete()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.filter_queryset(self.get_queryset())
        tasks = Task.objects.filter(assignees__in=qs).distinct()
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
        if self.request.user.is_superuser or self.request.user.role in (User.Role.OWNER, User.Role.ADMIN):
            return qs
        return qs.filter(pk=self.request.user.department_id)

    def perform_create(self, serializer):
        if not (self.request.user.is_superuser or self.request.user.role in (User.Role.OWNER, User.Role.ADMIN)):
            raise PermissionDenied("Only an Owner or Admin can create departments.")
        serializer.save()

    def perform_update(self, serializer):
        if not (self.request.user.is_superuser or self.request.user.role in (User.Role.OWNER, User.Role.ADMIN)):
            raise PermissionDenied("Only an Owner or Admin can update departments.")
        serializer.save()

    def perform_destroy(self, instance):
        if not (self.request.user.is_superuser or self.request.user.role in (User.Role.OWNER, User.Role.ADMIN)):
            raise PermissionDenied("Only an Owner or Admin can delete departments.")
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
        if user.role in (User.Role.OWNER, User.Role.ADMIN):
            qs = Task.objects.all()
        elif user.role == User.Role.MANAGER:
            qs = Task.objects.filter(department=user.department)
        else:
            qs = Task.objects.filter(department=user.department, assignees=user)
        qs = qs.select_related("department", "created_by").prefetch_related("assignees").distinct()
        if self.department_id():
            qs = qs.filter(department_id=self.department_id())
        if self.request.query_params.get("my_tasks", "").lower() in ("1", "true", "yes"):
            qs = qs.filter(assignees=self.request.user)
        return qs

    def perform_create(self, serializer):
        department = serializer.validated_data["department"]
        self.ensure_department_task_manager(department)
        serializer.save(created_by=self.request.user)

    def ensure_department_task_manager(self, department):
        user = self.request.user
        can_manage = user.is_active and (user.role in (User.Role.OWNER, User.Role.ADMIN) or (user.role == User.Role.MANAGER and user.department_id == department.id))
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
        if not user.is_active or not (user.role in (User.Role.OWNER, User.Role.ADMIN) or user.department_id == target_department.id):
            raise PermissionDenied("You are not a member of this department.")

        is_manager = user.role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)
        changed_fields = set(serializer.validated_data)
        member_fields = {"status", "progress"}
        if not is_manager:
            if not task.assignees.filter(pk=self.request.user.pk).exists():
                raise PermissionDenied("You can update only tasks assigned to you.")
            if not changed_fields.issubset(member_fields):
                raise PermissionDenied(
                    "Members can update only status and progress."
                )

        previous = serializer.instance.status
        task = serializer.save()
        if task.status == Task.Status.COMPLETED and previous != Task.Status.COMPLETED:
            task.progress, task.completed_at = 100, timezone.now()
            task.save(update_fields=["progress", "completed_at", "updated_at"])
        elif task.status != Task.Status.COMPLETED and previous == Task.Status.COMPLETED:
            task.completed_at = None
            task.save(update_fields=["completed_at", "updated_at"])

    def perform_destroy(self, instance):
        self.ensure_task_manager(instance)
        instance.delete()

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
        if self.request.user.role not in (User.Role.OWNER, User.Role.ADMIN):
            qs = qs.filter(department=self.request.user.department)
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
        if request.user.role not in (User.Role.OWNER, User.Role.ADMIN) and str(request.user.department_id) != department_id:
            return Response({"detail": "You are not a member of this department."}, status=status.HTTP_403_FORBIDDEN)
        tasks = Task.objects.filter(department_id=department_id)
        total = tasks.count()
        completed = tasks.filter(status=Task.Status.COMPLETED)
        completion_rate = round(completed.count() * 100 / total, 1) if total else 0
        monthly = list(tasks.annotate(month=TruncMonth("created_at")).values("month").annotate(created=Count("id"), completed=Count("id", filter=Q(status=Task.Status.COMPLETED))).order_by("month"))
        categories = list(tasks.values("category").annotate(count=Count("id")).order_by("-count"))
        overdue = tasks.exclude(status=Task.Status.COMPLETED).filter(due_date__lt=timezone.localdate()).count()
        velocity = completed.filter(completed_at__gte=timezone.now() - timedelta(days=7)).count()
        return Response({"task_completion_rate": completion_rate, "team_velocity": velocity, "overdue_tasks": overdue, "monthly_progress": monthly, "tasks_by_category": categories})


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
        qs = Conversation.objects.filter(participants=self.request.user).select_related("department").prefetch_related("participants", "messages__sender", "participant_links").distinct()
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
        serializer.save(sender=self.request.user)
        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())

    def perform_destroy(self, instance):
        if instance.sender != self.request.user:
            raise serializers.ValidationError("Only the sender can delete this message.")
        instance.is_deleted = True
        instance.body = ""
        instance.attachment.delete(save=False)
        instance.save(update_fields=["is_deleted", "body", "attachment", "updated_at"])


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
        if self.request.user.role not in (User.Role.OWNER, User.Role.ADMIN):
            qs = qs.filter(department=self.request.user.department)
        return qs.filter(department_id=self.department_id()) if self.department_id() else qs

    def perform_create(self, serializer):
        department = serializer.validated_data["department"]
        self.ensure_member(department)
        report = serializer.save(generated_by=self.request.user)
        tasks = Task.objects.filter(department=department)
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

