from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from import_export.admin import ImportExportMixin, ImportExportModelAdmin

from .forms import EventAdminForm
from .models import Conversation, ConversationParticipant, Department, Event, Message, Notification, Report, Task, TelegramIntegration, User, UserPreference
from .resources import (
    ConversationParticipantResource,
    ConversationResource,
    DepartmentResource,
    EventResource,
    MessageResource,
    ReportResource,
    TaskResource,
    UserPreferenceResource,
    UserResource,
)


@admin.register(User)
class TaskFlowUserAdmin(ImportExportMixin, UserAdmin):
    resource_classes = (UserResource,)
    fieldsets = UserAdmin.fieldsets + (("TaskFlow", {"fields": ("department", "accessible_departments", "has_all_departments_access", "role", "avatar", "phone", "job_title")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("TaskFlow", {"fields": ("email", "first_name", "last_name", "department", "accessible_departments", "has_all_departments_access", "role")}),)
    list_display = ("email", "username", "full_name", "department", "role", "has_all_departments_access", "job_title", "is_staff", "is_active")
    list_filter = ("role", "department", "has_all_departments_access", "is_active", "is_staff", "is_superuser", "groups")
    search_fields = ("email", "username", "first_name", "last_name", "phone", "job_title", "department__name", "accessible_departments__name")
    autocomplete_fields = ("department", "accessible_departments")
    ordering = ("email",)

    @admin.display(description="Full name", ordering="first_name")
    def full_name(self, obj):
        return obj.get_full_name() or "—"


@admin.register(Department)
class DepartmentAdmin(ImportExportModelAdmin):
    resource_classes = (DepartmentResource,)
    list_display = ("name", "code", "is_active", "member_count", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "description")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.users.filter(is_active=True).count()


@admin.register(Task)
class TaskAdmin(ImportExportModelAdmin):
    resource_classes = (TaskResource,)
    list_display = ("title", "department", "main_assignee", "status", "priority", "is_hidden", "progress", "due_date", "completed_at", "created_by")
    list_filter = ("is_hidden", "status", "priority", "department", "due_date")
    search_fields = ("title", "description", "category", "department__name", "assignees__email")
    autocomplete_fields = ("department", "assignees", "created_by")
    readonly_fields = ("main_assignee", "created_at", "updated_at", "completed_at")
    date_hierarchy = "due_date"


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient", "notification_type", "title", "read_at", "created_at")
    list_filter = ("notification_type", "read_at", "created_at")
    search_fields = ("recipient__email", "title", "body")
    autocomplete_fields = ("recipient", "actor", "task", "message")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TelegramIntegration)
class TelegramIntegrationAdmin(admin.ModelAdmin):
    list_display = ("user", "telegram_username", "telegram_user_id", "is_connected", "notifications_enabled", "connected_at")
    list_filter = ("is_connected", "notifications_enabled")
    search_fields = ("user__email", "telegram_username", "telegram_user_id")
    readonly_fields = ("telegram_user_id", "telegram_chat_id", "telegram_username", "connected_at", "created_at", "updated_at")


@admin.register(Event)
class EventAdmin(ImportExportModelAdmin):
    form = EventAdminForm
    resource_classes = (EventResource,)
    list_display = ("title", "department", "event_type", "starts_at", "ends_at", "location")
    list_filter = ("event_type", "department", "starts_at")
    search_fields = ("title", "description", "location", "department__name")
    autocomplete_fields = ("department", "attendees", "created_by")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "starts_at"


@admin.register(UserPreference)
class UserPreferenceAdmin(ImportExportModelAdmin):
    resource_classes = (UserPreferenceResource,)
    list_display = ("user", "theme", "language", "timezone", "email_notifications")
    list_filter = ("theme", "language", "email_notifications", "weekly_reports")
    search_fields = ("user__email", "user__first_name", "user__last_name", "timezone")
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Conversation)
class ConversationAdmin(ImportExportModelAdmin):
    resource_classes = (ConversationResource,)
    list_display = ("display_title", "department", "is_group", "participant_count", "updated_at")
    list_filter = ("is_group", "department")
    search_fields = ("title", "department__name", "participants__email")
    autocomplete_fields = ("department",)
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Conversation")
    def display_title(self, obj):
        return obj.title or "Untitled conversation"

    @admin.display(description="Participants")
    def participant_count(self, obj):
        return obj.participant_links.count()


@admin.register(ConversationParticipant)
class ConversationParticipantAdmin(ImportExportModelAdmin):
    resource_classes = (ConversationParticipantResource,)
    list_display = ("conversation", "user", "is_muted", "last_read_at")
    list_filter = ("is_muted",)
    search_fields = ("conversation__title", "user__email", "user__first_name", "user__last_name")
    autocomplete_fields = ("conversation", "user")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Message)
class MessageAdmin(ImportExportModelAdmin):
    resource_classes = (MessageResource,)
    list_display = ("short_body", "conversation", "sender", "is_deleted", "created_at")
    list_filter = ("is_deleted", "created_at")
    search_fields = ("body", "sender__email", "conversation__title")
    autocomplete_fields = ("conversation", "sender")
    readonly_fields = ("created_at", "updated_at", "edited_at")
    date_hierarchy = "created_at"

    @admin.display(description="Message")
    def short_body(self, obj):
        return obj.body[:60] + ("…" if len(obj.body) > 60 else "")


@admin.register(Report)
class ReportAdmin(ImportExportModelAdmin):
    resource_classes = (ReportResource,)
    list_display = ("name", "department", "report_type", "status", "generated_by", "created_at")
    list_filter = ("report_type", "status", "department", "created_at")
    search_fields = ("name", "department__name", "generated_by__email")
    autocomplete_fields = ("department", "generated_by")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


admin.site.site_header = "TaskFlow Administration"
admin.site.site_title = "TaskFlow Admin"
admin.site.index_title = "Administration Dashboard"
