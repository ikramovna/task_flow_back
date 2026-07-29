from import_export import resources

from .models import (
    Conversation,
    ConversationParticipant,
    Event,
    FAQ,
    Membership,
    Message,
    Project,
    Report,
    SupportTicket,
    Task,
    TimeEntry,
    User,
    UserPreference,
    Workspace,
)


class TaskFlowResource(resources.ModelResource):
    """Shared, safe defaults for TaskFlow admin imports."""

    class Meta:
        import_id_fields = ("id",)
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True


class UserResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = User
        exclude = ("password", "user_permissions")


class WorkspaceResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Workspace


class MembershipResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Membership


class ProjectResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = Project


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


class TimeEntryResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = TimeEntry


class FAQResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = FAQ


class SupportTicketResource(TaskFlowResource):
    class Meta(TaskFlowResource.Meta):
        model = SupportTicket
