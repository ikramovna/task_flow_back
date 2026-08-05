from django.contrib.auth.password_validation import validate_password
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from .models import Conversation, ConversationParticipant, Department, Event, Message, Report, Task, User, UserPreference


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

    def _task_counts(self, obj):
        qs = obj.assigned_tasks.filter(department=obj.department)
        return qs.count(), qs.filter(status=Task.Status.COMPLETED).count(), qs.filter(status=Task.Status.IN_PROGRESS).count()

    def get_efficiency(self, obj) -> int:
        total, done, _ = self._task_counts(obj)
        return round(done * 100 / total) if total else 0

    def get_completed_tasks(self, obj) -> int:
        return self._task_counts(obj)[1]

    def get_in_progress_tasks(self, obj) -> int:
        return self._task_counts(obj)[2]


class TaskSerializer(serializers.ModelSerializer):
    assignee_details = UserBriefSerializer(source="assignees", many=True, read_only=True)

    class Meta:
        model = Task
        fields = ("id", "department", "title", "description", "status", "priority", "category", "assignees", "assignee_details", "created_by", "due_date", "completed_at", "progress", "created_at", "updated_at")
        read_only_fields = ("created_by", "completed_at")

    def validate(self, attrs):
        department = attrs.get("department") or self.instance.department
        assignees = attrs.get("assignees", self.instance.assignees.all() if self.instance else [])
        for user in assignees:
            if user.department_id != department.id or not user.is_active:
                raise serializers.ValidationError({"assignees": "Every assignee must belong to the task department."})
        return attrs


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
        for user in attrs.get("attendees", []):
            if user.department_id != department.id or not user.is_active:
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


class ConversationSerializer(serializers.ModelSerializer):
    participant_details = UserBriefSerializer(source="participants", many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "department", "title", "is_group", "participants", "participant_details", "last_message", "unread_count", "created_at", "updated_at")

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

