from django.contrib.auth.password_validation import validate_password
from django.db.models import Avg
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from .models import Conversation, ConversationParticipant, Department, Event, Message, Notification, Project, Report, Task, User, UserPreference


class SafeTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        try:
            return super().validate(attrs)
        except User.DoesNotExist as exc:
            raise AuthenticationFailed(
                self.error_messages["no_active_account"],
                code="no_active_account",
            ) from exc


class SupportBotMessageSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=3000, trim_whitespace=True)
    screenshot = serializers.FileField(required=False, allow_null=True)

    def validate_screenshot(self, value):
        if value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("Screenshot must not exceed 5 MB.")
        if value.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise serializers.ValidationError("Only JPEG, PNG, and WebP screenshots are supported.")
        return value


class UserBriefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "avatar", "phone", "job_title", "department", "role", "is_active", "last_seen_at")


class TaskCreatorSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "full_name", "avatar")


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


class ProjectBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name", "status", "priority")


class ProjectSerializer(serializers.ModelSerializer):
    manager_detail = UserBriefSerializer(source="manager", read_only=True)
    team_member_details = UserBriefSerializer(source="team_members", many=True, read_only=True)
    created_by_detail = UserBriefSerializer(source="created_by", read_only=True)
    progress = serializers.SerializerMethodField()
    total_tasks = serializers.SerializerMethodField()
    completed_tasks = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = (
            "id", "department", "name", "description", "status", "priority",
            "category", "start_date", "end_date", "manager", "manager_detail",
            "team_members", "team_member_details", "progress", "total_tasks",
            "completed_tasks", "created_by", "created_by_detail", "created_at",
            "updated_at",
        )
        read_only_fields = ("created_by",)

    def validate(self, attrs):
        department = attrs.get("department", getattr(self.instance, "department", None))
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "End date cannot be before start date."})

        users = list(
            attrs.get(
                "team_members",
                self.instance.team_members.all() if self.instance else [],
            )
        )
        manager = attrs.get("manager", getattr(self.instance, "manager", None))
        if manager:
            users.append(manager)
        for user in users:
            if not user.is_active:
                raise serializers.ValidationError({"team_members": "Every project member must be active."})
            if department and user.department_id != department.id:
                raise serializers.ValidationError({"team_members": "Every project member must belong to the project department."})
        return attrs

    def create(self, validated_data):
        project = super().create(validated_data)
        if project.manager_id:
            project.team_members.add(project.manager)
        return project

    def update(self, instance, validated_data):
        project = super().update(instance, validated_data)
        if project.manager_id:
            project.team_members.add(project.manager)
        return project

    def get_progress(self, obj) -> int:
        value = getattr(obj, "task_progress", None)
        if value is None:
            value = obj.tasks.aggregate(value=Avg("progress"))["value"]
        return round(value or 0)

    def get_total_tasks(self, obj) -> int:
        value = getattr(obj, "task_count", None)
        return value if value is not None else obj.tasks.count()

    def get_completed_tasks(self, obj) -> int:
        value = getattr(obj, "completed_task_count", None)
        return value if value is not None else obj.tasks.filter(status=Task.Status.COMPLETED).count()


class TaskSerializer(serializers.ModelSerializer):
    assignee_details = UserBriefSerializer(source="assignees", many=True, read_only=True)
    main_assignee_detail = UserBriefSerializer(source="main_assignee", read_only=True)
    created_by_detail = TaskCreatorSerializer(source="created_by", read_only=True)
    project_detail = ProjectBriefSerializer(source="project", read_only=True)

    class Meta:
        model = Task
        fields = ("id", "department", "project", "project_detail", "title", "description", "status", "priority", "category", "effort_score", "assignees", "assignee_details", "main_assignee", "main_assignee_detail", "created_by", "created_by_detail", "due_date", "completed_at", "is_hidden", "is_archived", "archived_at", "archived_by", "progress", "created_at", "updated_at")
        extra_kwargs = {"department": {"required": False}, "project": {"required": False, "allow_null": True}}
        read_only_fields = ("main_assignee", "created_by", "completed_at", "is_archived", "archived_at", "archived_by")

    def validate(self, attrs):
        assignees = attrs.get("assignees", self.instance.assignees.all() if self.instance else [])
        department = attrs.get("department")
        project = attrs.get("project", getattr(self.instance, "project", None))
        if department is None and project is not None:
            department = project.department
            attrs["department"] = department
        if department is None and "assignees" in attrs:
            main_assignee = assignees[0] if assignees else None
            department = main_assignee.department if main_assignee else None
            if department is not None:
                attrs["department"] = department
        if department is None and self.instance is not None:
            department = self.instance.department
        if department is None:
            raise serializers.ValidationError({
                "assignees": "Select at least one assignee with a department."
            })
        if project is not None and project.department_id != department.id:
            raise serializers.ValidationError({"project": "Project must belong to the task department."})
        requester = self.context["request"].user
        assignees_to_validate = assignees if self.instance is None or "assignees" in attrs else []
        for user in assignees_to_validate:
            if not user.is_active:
                raise serializers.ValidationError({"assignees": "Every assignee must be active."})
            if (
                user.department_id != department.id
                and requester.role not in (
                    User.Role.OWNER,
                    User.Role.ADMIN,
                    User.Role.MANAGER,
                )
                and not requester.can_access_department(user.department_id)
            ):
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
        for user in attrs.get("attendees", []):
            if not user.is_active:
                raise serializers.ValidationError({"attendees": "Every attendee must be active."})
            if user.department_id != department.id and not requester.can_access_department(user.department_id):
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
    # DRF treats many-to-many fields with an explicit through model as
    # read-only unless the serializer field is declared explicitly. Without
    # this declaration incoming recipient IDs are silently discarded.
    participants = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        many=True,
        required=False,
    )
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
        department = attrs.get("department") or getattr(self.instance, "department", None)
        if department is None:
            raise serializers.ValidationError({"department": "This field is required."})
        participants = attrs.get("participants")
        if self.instance is None and not participants:
            raise serializers.ValidationError({"participants": "At least one recipient is required."})
        for user in participants or []:
            if user.department_id != department.id or not user.is_active:
                raise serializers.ValidationError({"participants": "Every participant must belong to the department."})
        if self.instance is None and not attrs.get("is_group", False):
            request = self.context.get("request")
            requester_id = request.user.pk if request and request.user.is_authenticated else None
            recipient_ids = {user.pk for user in participants if user.pk != requester_id}
            if len(recipient_ids) != 1:
                raise serializers.ValidationError(
                    {"participants": "A direct conversation requires exactly one recipient."}
                )
        return attrs

    def create(self, validated_data):
        # ConversationParticipant rows are created by the view after it has
        # added the requesting user to the recipient set.
        validated_data.pop("participants", None)
        return Conversation.objects.create(**validated_data)


class ReportSerializer(serializers.ModelSerializer):
    generated_by_detail = UserBriefSerializer(source="generated_by", read_only=True)

    class Meta:
        model = Report
        fields = ("id", "department", "name", "report_type", "status", "parameters", "result", "file", "generated_by", "generated_by_detail", "created_at", "updated_at")
        read_only_fields = ("status", "result", "file", "generated_by")
