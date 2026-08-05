from django.contrib.auth.hashers import identify_hasher, make_password
from django.utils.text import slugify
from import_export import resources

from .models import (
    Conversation,
    ConversationParticipant,
    Event,
    Department,
    Message,
    Report,
    Task,
    User,
    UserPreference,
)


class TaskFlowResource(resources.ModelResource):
    """Shared, safe defaults for TaskFlow admin imports."""

    class Meta:
        import_id_fields = ("id",)
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True


class UserResource(TaskFlowResource):
    def before_import_row(self, row, **kwargs):
        password = row.get("password")
        if not password:
            row["password"] = make_password(None)
            return

        try:
            identify_hasher(password)
        except ValueError:
            row["password"] = make_password(password)

    def get_export_fields(self, selected_fields=None):
        return [
            field
            for field in super().get_export_fields(selected_fields)
            if field.column_name != "password"
        ]

    class Meta(TaskFlowResource.Meta):
        model = User
        import_id_fields = ("email",)
        exclude = ("user_permissions",)


class DepartmentResource(TaskFlowResource):
    def before_import_row(self, row, **kwargs):
        if not row.get("code") and row.get("name"):
            row["code"] = slugify(row["name"])[:32]

    class Meta(TaskFlowResource.Meta):
        model = Department
        import_id_fields = ("code",)
        fields = (
            "id",
            "name",
            "code",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        )
        export_order = fields


class TaskResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Task


class EventResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Event


class UserPreferenceResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = UserPreference


class ConversationResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Conversation


class ConversationParticipantResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = ConversationParticipant


class MessageResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Message


class ReportResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Report

