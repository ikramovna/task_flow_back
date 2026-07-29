from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Conversation, ConversationParticipant, Event, FAQ, Membership, Message, Project, Report, SupportTicket, Task, TimeEntry, User, UserPreference, Workspace


@admin.register(User)
class TaskFlowUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("TaskFlow", {"fields": ("avatar", "phone", "job_title")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("TaskFlow", {"fields": ("email", "first_name", "last_name")}),)
    list_display = ("email", "username", "first_name", "last_name", "is_active")


admin.site.register(Workspace)
admin.site.register(Membership)
admin.site.register(Project)
admin.site.register(Task)
admin.site.register(Event)
admin.site.register(UserPreference)
admin.site.register(Conversation)
admin.site.register(ConversationParticipant)
admin.site.register(Message)
admin.site.register(Report)
admin.site.register(TimeEntry)
admin.site.register(FAQ)
admin.site.register(SupportTicket)
