import uuid

from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


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
    accessible_departments = models.ManyToManyField(
        "Department",
        blank=True,
        related_name="users_with_access",
        help_text="Additional departments this user may access. The primary department is always included.",
    )
    has_all_departments_access = models.BooleanField(
        default=False,
        help_text="Allow access to every department, including departments created later.",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    def can_access_department(self, department) -> bool:
        department_id = getattr(department, "pk", department)
        if self.is_superuser or self.has_all_departments_access:
            return True
        if self.department_id is not None and str(self.department_id) == str(department_id):
            return True
        return self.accessible_departments.filter(pk=department_id).exists()


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


class Project(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNING = "planning", "Planning"
        ACTIVE = "active", "Active"
        ON_HOLD = "on_hold", "On Hold"
        COMPLETED = "completed", "Completed"
        ARCHIVED = "archived", "Archived"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=220)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNING)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    category = models.CharField(max_length=80, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="managed_projects",
        null=True,
        blank=True,
    )
    team_members = models.ManyToManyField(User, blank=True, related_name="projects")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_projects")

    class Meta:
        ordering = ("end_date", "-created_at")
        indexes = [
            models.Index(fields=("department", "status"), name="project_dept_status_idx"),
            models.Index(fields=("department", "end_date"), name="project_dept_end_idx"),
        ]

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
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        related_name="tasks",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=220)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    category = models.CharField(max_length=80, blank=True)
    assignees = models.ManyToManyField(User, blank=True, related_name="assigned_tasks")
    main_assignee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="main_tasks",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_tasks")
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_hidden = models.BooleanField(default=False)
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
    effort_score = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Relative task complexity/effort from 1 (small) to 5 (very large).",
    )

    class Meta:
        ordering = ["due_date", "-created_at"]
        indexes = [
            models.Index(fields=("department", "is_archived", "status"), name="task_dept_arch_status_idx"),
            models.Index(fields=("department", "is_archived", "due_date"), name="task_dept_arch_due_idx"),
        ]

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        synchronized_fields = set(update_fields) if update_fields is not None else None

        if self.status == self.Status.COMPLETED:
            if self.completed_at is None:
                self.completed_at = timezone.now()
                if synchronized_fields is not None:
                    synchronized_fields.add("completed_at")
            if self.progress != 100:
                self.progress = 100
                if synchronized_fields is not None:
                    synchronized_fields.add("progress")
        elif self.completed_at is not None:
            self.completed_at = None
            if synchronized_fields is not None:
                synchronized_fields.add("completed_at")

        if synchronized_fields is not None:
            kwargs["update_fields"] = synchronized_fields
        return super().save(*args, **kwargs)


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
        indexes = [
            models.Index(fields=("department", "starts_at"), name="event_dept_starts_idx"),
        ]


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


class UserPresenceSession(TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="presence_sessions")
    channel_name = models.CharField(max_length=255, unique=True)
    last_heartbeat = models.DateTimeField(default=timezone.now, db_index=True)


class Message(TimeStampedModel):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sent_messages")
    body = models.TextField(blank=True)
    attachment = models.FileField(upload_to="message_attachments/%Y/%m/", blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=("conversation", "is_deleted", "created_at"), name="msg_conv_del_created_idx"),
        ]


class Notification(TimeStampedModel):
    class Type(models.TextChoices):
        TASK_ASSIGNED = "task_assigned", "Task Assigned"
        DEADLINE_REMINDER = "deadline_reminder", "Deadline Reminder"
        TASK_OVERDUE = "task_overdue", "Task Overdue"
        TASK_COMPLETED = "task_completed", "Task Completed"
        NEW_MESSAGE = "new_message", "New Message"

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name="triggered_notifications", null=True, blank=True
    )
    notification_type = models.CharField(max_length=24, choices=Type.choices)
    title = models.CharField(max_length=180)
    body = models.TextField(blank=True)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="notifications", null=True, blank=True)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="notifications", null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    event_key = models.CharField(max_length=120, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("recipient", "event_key"),
                condition=models.Q(event_key__isnull=False),
                name="unique_notification_event_per_recipient",
            )
        ]
        indexes = [models.Index(fields=("recipient", "read_at", "created_at"))]


class TelegramIntegration(TimeStampedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="telegram_integration")
    telegram_user_id = models.BigIntegerField(unique=True, null=True, blank=True)
    telegram_chat_id = models.BigIntegerField(unique=True, null=True, blank=True)
    telegram_username = models.CharField(max_length=255, blank=True)
    link_token = models.CharField(max_length=128, unique=True, null=True, blank=True)
    link_token_expires_at = models.DateTimeField(null=True, blank=True)
    is_connected = models.BooleanField(default=False)
    notifications_enabled = models.BooleanField(default=True)
    connected_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Telegram: {self.user.email}"


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
