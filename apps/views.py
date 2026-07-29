import csv
import io
from datetime import timedelta

from django.core.files.base import ContentFile
from django.http import FileResponse
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, inline_serializer

from .filters import EventFilter, ProjectFilter, TaskFilter
from .models import Conversation, ConversationParticipant, Event, FAQ, Membership, Message, Project, Report, SupportTicket, Task, TimeEntry, UserPreference, Workspace
from .pagination import StandardPagination
from .permissions import IsWorkspaceMember
from .serializers import AccountDeleteSerializer, ConversationSerializer, EventSerializer, FAQSerializer, MembershipSerializer, MessageSerializer, PasswordChangeSerializer, ProfileSerializer, ProjectSerializer, RegisterSerializer, ReportSerializer, SupportTicketSerializer, TaskSerializer, TimeEntrySerializer, TwoFactorSerializer, UserPreferenceSerializer, WorkspaceSerializer


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = (AllowAny,)


class WorkspaceViewSet(viewsets.ModelViewSet):
    queryset = Workspace.objects.none()
    serializer_class = WorkspaceSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return Workspace.objects.filter(memberships__user=self.request.user, memberships__is_active=True).annotate(member_count=Count("memberships", filter=Q(memberships__is_active=True))).distinct()


class WorkspaceScopedMixin:
    permission_classes = (IsAuthenticated, IsWorkspaceMember)
    pagination_class = StandardPagination

    def workspace_id(self):
        return self.request.query_params.get("workspace")

    def ensure_member(self, workspace):
        if not workspace.memberships.filter(user=self.request.user, is_active=True).exists():
            raise serializers.ValidationError({"workspace": "You are not a member of this workspace."})


class MembershipViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Membership.objects.none()
    serializer_class = MembershipSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_fields = ("role", "is_active")
    search_fields = ("user__first_name", "user__last_name", "user__email", "user__job_title")
    ordering_fields = ("joined_at", "user__first_name")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Membership.objects.select_related("user", "workspace").filter(workspace__memberships__user=self.request.user, workspace__memberships__is_active=True)
        return qs.filter(workspace_id=self.workspace_id()) if self.workspace_id() else qs

    def perform_create(self, serializer):
        self.ensure_member(serializer.validated_data["workspace"])
        serializer.save()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.filter_queryset(self.get_queryset())
        tasks = Task.objects.filter(project__workspace_id=self.workspace_id(), assignees__memberships__in=qs).distinct()
        total = qs.count()
        completed = tasks.filter(status=Task.Status.COMPLETED).count()
        efficiency = round(completed * 100 / tasks.count()) if tasks.exists() else 0
        return Response({"total_members": total, "average_efficiency": efficiency, "active_tasks": tasks.exclude(status=Task.Status.COMPLETED).count()})


class ProjectViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Project.objects.none()
    serializer_class = ProjectSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_class = ProjectFilter
    search_fields = ("name", "description")
    ordering_fields = ("name", "due_date", "created_at", "status")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Project.objects.filter(workspace__memberships__user=self.request.user, workspace__memberships__is_active=True).select_related("workspace", "created_by").prefetch_related("members").annotate(task_count=Count("tasks", distinct=True), completed_task_count=Count("tasks", filter=Q(tasks__status=Task.Status.COMPLETED), distinct=True)).distinct()
        return qs.filter(workspace_id=self.workspace_id()) if self.workspace_id() else qs

    def perform_create(self, serializer):
        self.ensure_member(serializer.validated_data["workspace"])
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.get_queryset()
        return Response({"active_projects": qs.exclude(status=Project.Status.ARCHIVED).count(), "in_progress": qs.filter(status=Project.Status.IN_PROGRESS).count(), "completed": qs.filter(status=Project.Status.COMPLETED).count(), "at_risk": qs.filter(status=Project.Status.AT_RISK).count()})


class TaskViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Task.objects.none()
    serializer_class = TaskSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_class = TaskFilter
    search_fields = ("title", "description", "project__name")
    ordering_fields = ("title", "due_date", "priority", "status", "created_at", "progress")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Task.objects.filter(project__workspace__memberships__user=self.request.user, project__workspace__memberships__is_active=True).select_related("project", "created_by").prefetch_related("assignees").distinct()
        return qs.filter(project__workspace_id=self.workspace_id()) if self.workspace_id() else qs

    def perform_create(self, serializer):
        self.ensure_member(serializer.validated_data["project"].workspace)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        previous = serializer.instance.status
        task = serializer.save()
        if task.status == Task.Status.COMPLETED and previous != Task.Status.COMPLETED:
            task.progress, task.completed_at = 100, timezone.now()
            task.save(update_fields=["progress", "completed_at", "updated_at"])
        elif task.status != Task.Status.COMPLETED and previous == Task.Status.COMPLETED:
            task.completed_at = None
            task.save(update_fields=["completed_at", "updated_at"])


class EventViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Event.objects.none()
    serializer_class = EventSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_class = EventFilter
    search_fields = ("title", "description", "location")
    ordering_fields = ("starts_at", "ends_at", "created_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Event.objects.filter(workspace__memberships__user=self.request.user, workspace__memberships__is_active=True).select_related("workspace", "created_by").prefetch_related("attendees").distinct()
        return qs.filter(workspace_id=self.workspace_id()) if self.workspace_id() else qs

    def perform_create(self, serializer):
        self.ensure_member(serializer.validated_data["workspace"])
        serializer.save(created_by=self.request.user)


class AnalyticsView(APIView):
    permission_classes = (IsAuthenticated, IsWorkspaceMember)

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
        workspace_id = request.query_params.get("workspace")
        if not workspace_id:
            return Response({"workspace": "This query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.memberships.filter(workspace_id=workspace_id, is_active=True).exists():
            return Response({"detail": "You are not a member of this workspace."}, status=status.HTTP_403_FORBIDDEN)
        tasks = Task.objects.filter(project__workspace_id=workspace_id)
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


class ConversationViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Conversation.objects.none()
    serializer_class = ConversationSerializer
    filter_backends = (filters.SearchFilter, filters.OrderingFilter)
    search_fields = ("title", "participants__first_name", "participants__last_name", "participants__email")
    ordering_fields = ("updated_at", "created_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Conversation.objects.filter(participants=self.request.user).select_related("workspace").prefetch_related("participants", "messages__sender", "participant_links").distinct()
        return qs.filter(workspace_id=self.workspace_id()) if self.workspace_id() else qs

    @transaction.atomic
    def perform_create(self, serializer):
        workspace = serializer.validated_data["workspace"]
        self.ensure_member(workspace)
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


class ReportViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Report.objects.none()
    serializer_class = ReportSerializer
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter)
    filterset_fields = ("report_type", "status")
    search_fields = ("name",)
    ordering_fields = ("created_at", "name", "status")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        qs = Report.objects.filter(workspace__memberships__user=self.request.user, workspace__memberships__is_active=True).select_related("generated_by", "workspace").distinct()
        return qs.filter(workspace_id=self.workspace_id()) if self.workspace_id() else qs

    def perform_create(self, serializer):
        workspace = serializer.validated_data["workspace"]
        self.ensure_member(workspace)
        report = serializer.save(generated_by=self.request.user)
        tasks = Task.objects.filter(project__workspace=workspace)
        result = {"projects": workspace.projects.count(), "tasks": tasks.count(), "completed": tasks.filter(status=Task.Status.COMPLETED).count(), "in_progress": tasks.filter(status=Task.Status.IN_PROGRESS).count(), "members": workspace.memberships.filter(is_active=True).count(), "logged_minutes": sum(entry.minutes for entry in TimeEntry.objects.filter(task__project__workspace=workspace))}
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


class FAQViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FAQ.objects.filter(is_active=True)
    serializer_class = FAQSerializer
    permission_classes = (AllowAny,)
    pagination_class = None
    filter_backends = (filters.SearchFilter,)
    search_fields = ("question", "answer", "category")


class TimeEntryViewSet(viewsets.ModelViewSet):
    queryset = TimeEntry.objects.none()
    serializer_class = TimeEntrySerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPagination
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_fields = ("task", "user")
    ordering_fields = ("started_at", "ended_at")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return TimeEntry.objects.filter(task__project__workspace__memberships__user=self.request.user, task__project__workspace__memberships__is_active=True).select_related("task", "user").distinct()

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        if not task.project.workspace.memberships.filter(user=self.request.user, is_active=True).exists():
            raise serializers.ValidationError({"task": "You are not a member of this workspace."})
        serializer.save(user=self.request.user)

    def _ensure_owner_or_manager(self, instance):
        allowed = instance.task.project.workspace.memberships.filter(user=self.request.user, is_active=True, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.MANAGER]).exists()
        if instance.user != self.request.user and not allowed:
            raise PermissionDenied("Only the owner or a workspace manager can change this entry.")

    def perform_update(self, serializer):
        self._ensure_owner_or_manager(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._ensure_owner_or_manager(instance)
        instance.delete()


class SupportTicketViewSet(viewsets.ModelViewSet):
    queryset = SupportTicket.objects.none()
    serializer_class = SupportTicketSerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPagination
    http_method_names = ("get", "post", "head", "options")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return SupportTicket.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
