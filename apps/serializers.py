from django.contrib.auth.password_validation import validate_password
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from .models import Conversation, ConversationParticipant, Department, Event, Message, Notification, Report, Task, User, UserPreference


class UserBriefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "avatar", "phone", "job_title", "department", "role", "is_active")


class ProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "username", "first_name", "last_name", "full_name", "avatar", "phone", "job_title", "department", "role", "is_active", "two_factor_enabled", "date_joined")
        read_only_fields = ("id", "email", "username", "department", "role", "is_active", "two_factor_enabled", "date_joined")


class DepartmentSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Department
        fields = ("id", "name", "code", "description", "is_active", "member_count", "created_at", "updated_at")


class MemberSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    efficiency = serializers.SerializerMethodField()
    completed_tasks = serializers.SerializerMethodField()
    in_progress_tasks = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "email", "username", "password", "first_name", "last_name", "full_name", "avatar", "phone", "job_title", "department", "role", "is_active", "date_joined", "efficiency", "completed_tasks", "in_progress_tasks")
        read_only_fields = ("date_joined",)

    def validate_department(self, value):
        if value is None:
            raise serializers.ValidationError("This field is required.")
        return value

    def validate(self, attrs):
        if self.instance is None and not attrs.get("department"):
            raise serializers.ValidationError({"department": "This field is required."})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        user.set_password(password) if password else user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        instance = super().update(instance, validated_data)
        if password:
            instance.set_password(password)
            instance.save(update_fields=["password"])
        return instance

    def get_efficiency(self, obj) -> int:
        total = getattr(obj, "task_count", 0)
        done = getattr(obj, "completed_task_count", 0)
        return round(done * 100 / total) if total else 0

    def get_completed_tasks(self, obj) -> int:
        return getattr(obj, "completed_task_count", 0)

    def get_in_progress_tasks(self, obj) -> int:
        return getattr(obj, "in_progress_task_count", 0)


class TaskSerializer(serializers.ModelSerializer):
    assignee_details = UserBriefSerializer(source="assignees", many=True, read_only=True)
    main_assignee_detail = UserBriefSerializer(source="main_assignee", read_only=True)

    class Meta:
        model = Task
        fields = ("id", "department", "title", "description", "status", "priority", "category", "assignees", "assignee_details", "main_assignee", "main_assignee_detail", "created_by", "due_date", "completed_at", "is_hidden", "is_archived", "archived_at", "archived_by", "progress", "created_at", "updated_at")
        read_only_fields = ("main_assignee", "created_by", "completed_at", "is_archived", "archived_at", "archived_by")

    def validate(self, attrs):
        department = attrs.get("department") or self.instance.department
        assignees = attrs.get("assignees", self.instance.assignees.all() if self.instance else [])
        requester = self.context["request"].user
        can_assign_across_departments = requester.role in (
            User.Role.OWNER,
            User.Role.ADMIN,
            User.Role.MANAGER,
        )
        for user in assignees:
            if not user.is_active:
                raise serializers.ValidationError({"assignees": "Every assignee must be active."})
            if not can_assign_across_departments and user.department_id != department.id:
                raise serializers.ValidationError({"assignees": "Every assignee must belong to the task department."})
        return attrs

    def create(self, validated_data):
        assignees = validated_data.pop("assignees", [])
        task = Task.objects.create(
            **validated_data,
            main_assignee=assignees[0] if assignees else None,
        )
        task.assignees.set(assignees)
        return task

    def update(self, instance, validated_data):
        assignees = validated_data.pop("assignees", None)
        instance = super().update(instance, validated_data)
        if assignees is not None:
            instance.assignees.set(assignees)
            instance.main_assignee = assignees[0] if assignees else None
            instance.save(update_fields=("main_assignee", "updated_at"))
        return instance


class EventSerializer(serializers.ModelSerializer):
    attendee_details = UserBriefSerializer(source="attendees", many=True, read_only=True)

    class Meta:
        model = Event
        fields = ("id", "department", "title", "event_type", "description", "starts_at", "ends_at", "location", "meeting_url", "attendees", "attendee_details", "created_by", "created_at", "updated_at")
        read_only_fields = ("created_by",)

    def validate(self, attrs):
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "Event must end after it starts."})
        department = attrs.get("department") or self.instance.department
        requester = self.context["request"].user
        can_invite_across_departments = requester.role in (
            User.Role.OWNER,
            User.Role.ADMIN,
            User.Role.MANAGER,
        )
        for user in attrs.get("attendees", []):
            if not user.is_active:
                raise serializers.ValidationError({"attendees": "Every attendee must be active."})
            if not can_invite_across_departments and user.department_id != department.id:
                raise serializers.ValidationError({"attendees": "Every attendee must belong to the department."})
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


class NotificationSerializer(serializers.ModelSerializer):
    actor_detail = UserBriefSerializer(source="actor", read_only=True)
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ("id", "notification_type", "title", "body", "actor_detail", "task", "message", "is_read", "read_at", "created_at")
        read_only_fields = fields

    def get_is_read(self, obj) -> bool:
        return obj.read_at is not None


class ConversationSerializer(serializers.ModelSerializer):
    participant_details = UserBriefSerializer(source="participants", many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "department", "title", "is_group", "participants", "participant_details", "last_message", "unread_count", "created_at", "updated_at")

    def get_last_message(self, obj) -> dict | None:
        messages = getattr(obj, "visible_messages", None)
        message = messages[-1] if messages else None
        return MessageSerializer(message, context=self.context).data if message else None

    def get_unread_count(self, obj) -> int:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return 0
        links = getattr(obj, "current_user_links", [])
        link = links[0] if links else None
        messages = getattr(obj, "visible_messages", [])
        return sum(
            message.sender_id != request.user.id
            and (not link or not link.last_read_at or message.created_at > link.last_read_at)
            for message in messages
        )

    def validate(self, attrs):
        department = attrs.get("department") or self.instance.department
        for user in attrs.get("participants", []):
            if user.department_id != department.id or not user.is_active:
                raise serializers.ValidationError({"participants": "Every participant must belong to the department."})
        return attrs


class ReportSerializer(serializers.ModelSerializer):
    generated_by_detail = UserBriefSerializer(source="generated_by", read_only=True)

    class Meta:
        model = Report
        fields = ("id", "department", "name", "report_type", "status", "parameters", "result", "file", "generated_by", "generated_by_detail", "created_at", "updated_at")
        read_only_fields = ("status", "result", "file", "generated_by")
