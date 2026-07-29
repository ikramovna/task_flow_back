import uuid

from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class User(AbstractUser):
    email = models.EmailField(unique=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    phone = models.CharField(max_length=32, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    two_factor_enabled = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]


class Workspace(TimeStampedModel):
    name = models.CharField(max_length=150)
    slug = models.SlugField(unique=True)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="owned_workspaces")
    members = models.ManyToManyField(User, through="Membership", related_name="workspaces")

    def __str__(self):
        return self.name


class Membership(TimeStampedModel):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MANAGER = "manager", "Manager"
        MEMBER = "member", "Member"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user"], name="unique_workspace_member")]


class Project(TimeStampedModel):
    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not Started"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        AT_RISK = "at_risk", "At Risk"
        ARCHIVED = "archived", "Archived"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    members = models.ManyToManyField(User, blank=True, related_name="projects")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_projects")

    class Meta:
        ordering = ["-created_at"]

    @property
    def progress(self):
        values = self.tasks.aggregate(total=models.Count("id"), done=models.Count("id", filter=models.Q(status=Task.Status.COMPLETED)))
        return round(values["done"] * 100 / values["total"]) if values["total"] else 0


class Task(TimeStampedModel):
    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not Started"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=220)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    category = models.CharField(max_length=80, blank=True)
    assignees = models.ManyToManyField(User, blank=True, related_name="assigned_tasks")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_tasks")
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    progress = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])

    class Meta:
        ordering = ["due_date", "-created_at"]


class Event(TimeStampedModel):
    class Type(models.TextChoices):
        MEETING = "meeting", "Meeting"
        REVIEW = "review", "Review"
        DEMO = "demo", "Demo"
        DEADLINE = "deadline", "Deadline"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="events")
    title = models.CharField(max_length=180)
    event_type = models.CharField(max_length=12, choices=Type.choices, default=Type.MEETING)
    description = models.TextField(blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    location = models.CharField(max_length=250, blank=True)
    meeting_url = models.URLField(blank=True)
    attendees = models.ManyToManyField(User, blank=True, related_name="events")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_events")

    class Meta:
        ordering = ["starts_at"]


class UserPreference(TimeStampedModel):
    class Theme(models.TextChoices):
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"
        SYSTEM = "system", "System"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="preferences")
    email_notifications = models.BooleanField(default=True)
    task_updates = models.BooleanField(default=True)
    team_messages = models.BooleanField(default=True)
    weekly_reports = models.BooleanField(default=False)
    task_assigned = models.BooleanField(default=True)
    deadline_reminder = models.BooleanField(default=False)
    task_overdue = models.BooleanField(default=True)
    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.LIGHT)
    language = models.CharField(max_length=12, default="en-US")
    timezone = models.CharField(max_length=64, default="Asia/Tashkent")
    currency = models.CharField(max_length=3, default="USD")
    date_format = models.CharField(max_length=16, default="MM/DD/YYYY")


class Conversation(TimeStampedModel):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField(max_length=180, blank=True)
    is_group = models.BooleanField(default=False)
    participants = models.ManyToManyField(User, through="ConversationParticipant", related_name="conversations")

    class Meta:
        ordering = ["-updated_at"]


class ConversationParticipant(TimeStampedModel):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="participant_links")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="conversation_links")
    last_read_at = models.DateTimeField(null=True, blank=True)
    is_muted = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="unique_conversation_participant")]


class Message(TimeStampedModel):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sent_messages")
    body = models.TextField(blank=True)
    attachment = models.FileField(upload_to="message_attachments/%Y/%m/", blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]


class Report(TimeStampedModel):
    class Type(models.TextChoices):
        WEEKLY_PROGRESS = "weekly_progress", "Weekly Progress"
        TEAM_PERFORMANCE = "team_performance", "Team Performance"
        PROJECT_STATUS = "project_status", "Project Status"
        TIME_TRACKING = "time_tracking", "Time Tracking"
        CUSTOM = "custom", "Custom"

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="reports")
    name = models.CharField(max_length=180)
    report_type = models.CharField(max_length=24, choices=Type.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROCESSING)
    parameters = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    file = models.FileField(upload_to="reports/%Y/%m/", blank=True)
    generated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="generated_reports")

    class Meta:
        ordering = ["-created_at"]


class TimeEntry(TimeStampedModel):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="time_entries")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="time_entries")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    note = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def minutes(self):
        if not self.started_at or not self.ended_at:
            return 0
        return max(0, round((self.ended_at - self.started_at).total_seconds() / 60))


class FAQ(TimeStampedModel):
    question = models.CharField(max_length=250)
    answer = models.TextField()
    category = models.CharField(max_length=80, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "question"]


class SupportTicket(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_tickets")
    subject = models.CharField(max_length=200)
    message = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)

    class Meta:
        ordering = ["-created_at"]
