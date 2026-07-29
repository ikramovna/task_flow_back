from django.db import transaction
from django.contrib.auth.password_validation import validate_password
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from .models import Conversation, ConversationParticipant, Event, FAQ, Membership, Message, Project, Report, SupportTicket, Task, TimeEntry, User, UserPreference, Workspace


class UserBriefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "avatar", "phone", "job_title")


class ProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "username", "first_name", "last_name", "full_name", "avatar", "phone", "job_title", "two_factor_enabled", "date_joined")
        read_only_fields = ("id", "email", "username", "two_factor_enabled", "date_joined")


class MembershipSerializer(serializers.ModelSerializer):
    user_detail = UserBriefSerializer(source="user", read_only=True)
    efficiency = serializers.SerializerMethodField()
    completed_tasks = serializers.SerializerMethodField()
    in_progress_tasks = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ("id", "workspace", "user", "user_detail", "role", "is_active", "joined_at", "efficiency", "completed_tasks", "in_progress_tasks")
        read_only_fields = ("joined_at",)

    def _task_counts(self, obj):
        qs = obj.user.assigned_tasks.filter(project__workspace=obj.workspace)
        return qs.count(), qs.filter(status=Task.Status.COMPLETED).count(), qs.filter(status=Task.Status.IN_PROGRESS).count()

    def get_efficiency(self, obj) -> int:
        total, done, _ = self._task_counts(obj)
        return round(done * 100 / total) if total else 0

    def get_completed_tasks(self, obj) -> int:
        return self._task_counts(obj)[1]

    def get_in_progress_tasks(self, obj) -> int:
        return self._task_counts(obj)[2]


class WorkspaceSerializer(serializers.ModelSerializer):
    owner_detail = UserBriefSerializer(source="owner", read_only=True)
    member_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Workspace
        fields = ("id", "name", "slug", "owner", "owner_detail", "member_count", "created_at")
        read_only_fields = ("owner",)

    @transaction.atomic
    def create(self, validated_data):
        user = self.context["request"].user
        workspace = Workspace.objects.create(owner=user, **validated_data)
        Membership.objects.create(workspace=workspace, user=user, role=Membership.Role.OWNER)
        return workspace


class ProjectSerializer(serializers.ModelSerializer):
    member_details = UserBriefSerializer(source="members", many=True, read_only=True)
    progress = serializers.IntegerField(read_only=True)
    task_count = serializers.IntegerField(read_only=True)
    completed_task_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Project
        fields = ("id", "workspace", "name", "description", "status", "priority", "start_date", "due_date", "members", "member_details", "created_by", "progress", "task_count", "completed_task_count", "created_at", "updated_at")
        read_only_fields = ("created_by",)

    def validate(self, attrs):
        workspace = attrs.get("workspace") or self.instance.workspace
        users = attrs.get("members", [])
        invalid = [u.id for u in users if not u.memberships.filter(workspace=workspace, is_active=True).exists()]
        if invalid:
            raise serializers.ValidationError({"members": "Every project member must belong to the workspace."})
        return attrs


class TaskSerializer(serializers.ModelSerializer):
    assignee_details = UserBriefSerializer(source="assignees", many=True, read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)

    class Meta:
        model = Task
        fields = ("id", "project", "project_name", "title", "description", "status", "priority", "category", "assignees", "assignee_details", "created_by", "due_date", "completed_at", "progress", "created_at", "updated_at")
        read_only_fields = ("created_by", "completed_at")

    def validate(self, attrs):
        project = attrs.get("project") or self.instance.project
        for user in attrs.get("assignees", []):
            if not user.memberships.filter(workspace=project.workspace, is_active=True).exists():
                raise serializers.ValidationError({"assignees": "Every assignee must belong to the project workspace."})
        return attrs


class EventSerializer(serializers.ModelSerializer):
    attendee_details = UserBriefSerializer(source="attendees", many=True, read_only=True)

    class Meta:
        model = Event
        fields = ("id", "workspace", "title", "event_type", "description", "starts_at", "ends_at", "location", "meeting_url", "attendees", "attendee_details", "created_by", "created_at", "updated_at")
        read_only_fields = ("created_by",)

    def validate(self, attrs):
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "Event must end after it starts."})
        workspace = attrs.get("workspace") or self.instance.workspace
        for user in attrs.get("attendees", []):
            if not user.memberships.filter(workspace=workspace, is_active=True).exists():
                raise serializers.ValidationError({"attendees": "Every attendee must belong to the workspace."})
        return attrs


class UserPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPreference
        exclude = ("id", "user", "created_at", "updated_at")


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        validate_password(attrs["new_password"], self.context["request"].user)
        return attrs


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})

        try:
            user_id = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=user_id, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({"token": "Reset link is invalid or expired."})

        validate_password(attrs["new_password"], user)
        attrs["user"] = user
        return attrs


class AccountDeleteSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)

    def validate_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Password is incorrect.")
        return value


class TwoFactorSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class MessageSerializer(serializers.ModelSerializer):
    sender_detail = UserBriefSerializer(source="sender", read_only=True)

    class Meta:
        model = Message
        fields = ("id", "conversation", "sender", "sender_detail", "body", "attachment", "edited_at", "is_deleted", "created_at")
        read_only_fields = ("sender", "edited_at", "is_deleted")

    def validate(self, attrs):
        if not attrs.get("body") and not attrs.get("attachment"):
            raise serializers.ValidationError("A message body or attachment is required.")
        return attrs


class ConversationSerializer(serializers.ModelSerializer):
    participant_details = UserBriefSerializer(source="participants", many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "workspace", "title", "is_group", "participants", "participant_details", "last_message", "unread_count", "created_at", "updated_at")

    def get_last_message(self, obj) -> dict | None:
        message = obj.messages.filter(is_deleted=False).last()
        return MessageSerializer(message, context=self.context).data if message else None

    def get_unread_count(self, obj) -> int:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return 0
        link = obj.participant_links.filter(user=request.user).first()
        qs = obj.messages.exclude(sender=request.user).filter(is_deleted=False)
        return qs.filter(created_at__gt=link.last_read_at).count() if link and link.last_read_at else qs.count()

    def validate(self, attrs):
        workspace = attrs.get("workspace") or self.instance.workspace
        for user in attrs.get("participants", []):
            if not user.memberships.filter(workspace=workspace, is_active=True).exists():
                raise serializers.ValidationError({"participants": "Every participant must belong to the workspace."})
        return attrs


class ReportSerializer(serializers.ModelSerializer):
    generated_by_detail = UserBriefSerializer(source="generated_by", read_only=True)

    class Meta:
        model = Report
        fields = ("id", "workspace", "name", "report_type", "status", "parameters", "result", "file", "generated_by", "generated_by_detail", "created_at", "updated_at")
        read_only_fields = ("status", "result", "file", "generated_by")


class TimeEntrySerializer(serializers.ModelSerializer):
    user_detail = UserBriefSerializer(source="user", read_only=True)
    minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = TimeEntry
        fields = ("id", "task", "user", "user_detail", "started_at", "ended_at", "minutes", "note", "created_at")
        read_only_fields = ("user",)

    def validate(self, attrs):
        started = attrs.get("started_at", getattr(self.instance, "started_at", None))
        ended = attrs.get("ended_at", getattr(self.instance, "ended_at", None))
        if started and ended and ended <= started:
            raise serializers.ValidationError({"ended_at": "End time must be after start time."})
        return attrs


class FAQSerializer(serializers.ModelSerializer):
    class Meta:
        model = FAQ
        fields = ("id", "question", "answer", "category")


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ("id", "subject", "message", "status", "priority", "created_at", "updated_at")
        read_only_fields = ("status",)
