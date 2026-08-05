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
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MANAGER = "manager", "Manager"
        MEMBER = "member", "Member"

    email = models.EmailField(unique=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    phone = models.CharField(max_length=32, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    two_factor_enabled = models.BooleanField(default=False)
    department = models.ForeignKey(
        "Department", on_delete=models.SET_NULL, related_name="users", null=True, blank=True
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]


class Department(TimeStampedModel):
    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=32, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        constraints = [models.UniqueConstraint(fields=("name",), name="unique_department_name")]

    def __str__(self):
        return self.name


class Task(TimeStampedModel):
    class Status(models.TextChoices):
        BACKLOG = "backlog", "Backlog"
        NOT_STARTED = "not_started", "Not Started"
        IN_PROGRESS = "in_progress", "In Progress"
        ON_HOLD = "on_hold", "On Hold"
        COMPLETED = "completed", "Completed"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=220)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    category = models.CharField(max_length=80, blank=True)
    assignees = models.ManyToManyField(User, blank=True, related_name="assigned_tasks")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_tasks")
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="archived_tasks",
        null=True,
        blank=True,
    )
    progress = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])

    class Meta:
        ordering = ["due_date", "-created_at"]


class Event(TimeStampedModel):
    class Type(models.TextChoices):
        MEETING = "meeting", "Meeting"
        REVIEW = "review", "Review"
        DEMO = "demo", "Demo"
        DEADLINE = "deadline", "Deadline"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="events", null=True, blank=True)
    title = models.CharField(max_length=180)
    event_type = models.CharField(max_length=80, default=Type.MEETING)
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
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="conversations", null=True, blank=True)
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
        DEPARTMENT_STATUS = "department_status", "Department Status"
        TIME_TRACKING = "time_tracking", "Time Tracking"
        CUSTOM = "custom", "Custom"

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="reports", null=True, blank=True)
    name = models.CharField(max_length=180)
    report_type = models.CharField(max_length=24, choices=Type.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROCESSING)
    parameters = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    file = models.FileField(upload_to="reports/%Y/%m/", blank=True)
    generated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="generated_reports")

    class Meta:
        ordering = ["-created_at"]
