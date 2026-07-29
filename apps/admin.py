from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Conversation, ConversationParticipant, Event, FAQ, Membership, Message, Project, Report, SupportTicket, Task, TimeEntry, User, UserPreference, Workspace


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ("user",)


@admin.register(User)
class TaskFlowUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("TaskFlow", {"fields": ("avatar", "phone", "job_title")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("TaskFlow", {"fields": ("email", "first_name", "last_name")}),)
    list_display = ("email", "username", "full_name", "job_title", "is_staff", "is_active")
    list_filter = ("is_active", "is_staff", "is_superuser", "groups")
    search_fields = ("email", "username", "first_name", "last_name", "phone", "job_title")
    ordering = ("email",)

    @admin.display(description="To‘liq ism", ordering="first_name")
    def full_name(self, obj):
        return obj.get_full_name() or "—"


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "member_count", "created_at")
    search_fields = ("name", "slug", "owner__email", "owner__first_name", "owner__last_name")
    autocomplete_fields = ("owner",)
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    inlines = (MembershipInline,)

    @admin.display(description="A’zolar")
    def member_count(self, obj):
        return obj.memberships.count()


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "role", "is_active", "joined_at")
    list_filter = ("role", "is_active", "workspace")
    search_fields = ("user__email", "user__first_name", "user__last_name", "workspace__name")
    autocomplete_fields = ("workspace", "user")
    readonly_fields = ("joined_at", "created_at", "updated_at")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "status", "priority", "progress", "due_date", "created_by")
    list_filter = ("status", "priority", "workspace", "due_date")
    search_fields = ("name", "description", "workspace__name", "created_by__email")
    autocomplete_fields = ("workspace", "members", "created_by")
    readonly_fields = ("created_at", "updated_at", "progress")
    date_hierarchy = "created_at"


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "status", "priority", "progress", "due_date", "created_by")
    list_filter = ("status", "priority", "project__workspace", "project", "due_date")
    search_fields = ("title", "description", "category", "project__name", "assignees__email")
    autocomplete_fields = ("project", "assignees", "created_by")
    readonly_fields = ("created_at", "updated_at", "completed_at")
    date_hierarchy = "due_date"


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "workspace", "event_type", "starts_at", "ends_at", "location")
    list_filter = ("event_type", "workspace", "starts_at")
    search_fields = ("title", "description", "location", "workspace__name")
    autocomplete_fields = ("workspace", "attendees", "created_by")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "starts_at"


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "theme", "language", "timezone", "email_notifications")
    list_filter = ("theme", "language", "email_notifications", "weekly_reports")
    search_fields = ("user__email", "user__first_name", "user__last_name", "timezone")
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("display_title", "workspace", "is_group", "participant_count", "updated_at")
    list_filter = ("is_group", "workspace")
    search_fields = ("title", "workspace__name", "participants__email")
    autocomplete_fields = ("workspace",)
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Suhbat")
    def display_title(self, obj):
        return obj.title or "Nomsiz suhbat"

    @admin.display(description="Ishtirokchilar")
    def participant_count(self, obj):
        return obj.participant_links.count()


@admin.register(ConversationParticipant)
class ConversationParticipantAdmin(admin.ModelAdmin):
    list_display = ("conversation", "user", "is_muted", "last_read_at")
    list_filter = ("is_muted",)
    search_fields = ("conversation__title", "user__email", "user__first_name", "user__last_name")
    autocomplete_fields = ("conversation", "user")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("short_body", "conversation", "sender", "is_deleted", "created_at")
    list_filter = ("is_deleted", "created_at")
    search_fields = ("body", "sender__email", "conversation__title")
    autocomplete_fields = ("conversation", "sender")
    readonly_fields = ("created_at", "updated_at", "edited_at")
    date_hierarchy = "created_at"

    @admin.display(description="Xabar")
    def short_body(self, obj):
        return obj.body[:60] + ("…" if len(obj.body) > 60 else "")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "report_type", "status", "generated_by", "created_at")
    list_filter = ("report_type", "status", "workspace", "created_at")
    search_fields = ("name", "workspace__name", "generated_by__email")
    autocomplete_fields = ("workspace", "generated_by")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("task", "user", "started_at", "ended_at", "minutes")
    list_filter = ("started_at", "user")
    search_fields = ("task__title", "user__email", "note")
    autocomplete_fields = ("task", "user")
    readonly_fields = ("created_at", "updated_at", "minutes")
    date_hierarchy = "started_at"


@admin.register(FAQ)
class FAQAdmin(admin.ModelAdmin):
    list_display = ("question", "category", "sort_order", "is_active", "updated_at")
    list_filter = ("is_active", "category")
    search_fields = ("question", "answer", "category")
    list_editable = ("sort_order", "is_active")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "user", "status", "priority", "created_at")
    list_filter = ("status", "priority", "created_at")
    search_fields = ("subject", "message", "user__email", "user__first_name", "user__last_name")
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


admin.site.site_header = "TaskFlow boshqaruv paneli"
admin.site.site_title = "TaskFlow Admin"
admin.site.index_title = "Boshqaruv paneli"
