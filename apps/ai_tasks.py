"""Shared text/voice task workflow for Tiko and Telegram."""
import hashlib
import re
import unicodedata
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from .ai_provider import extract_task, transcribe, validate_audio
from .models import AITaskRequest, Project, User
from .serializers import TaskSerializer
from .task_creation import save_task
from .task_visibility import PRIVILEGED_TASK_ROLES


@extend_schema_field(OpenApiTypes.BINARY)
class AudioUploadField(serializers.FileField):
    pass


class AITaskInputSerializer(serializers.Serializer):
    request_id = serializers.UUIDField()
    text = serializers.CharField(max_length=6000, required=False)
    audio = AudioUploadField(required=False)

    def validate_audio(self, value):
        return validate_audio(value)

    def validate(self, attrs):
        if bool(attrs.get("text")) == bool(attrs.get("audio")):
            raise serializers.ValidationError("Send either text or one audio file.")
        return attrs


class ExtractedTaskSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=220)
    description = serializers.CharField(max_length=6000, allow_blank=True)
    assignee = serializers.CharField(max_length=254)
    project = serializers.CharField(max_length=220, allow_null=True)
    due_date = serializers.DateField(allow_null=True)
    priority = serializers.ChoiceField(choices=("low", "medium", "high"))


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub("['‘’ʻʼ`-]", "", value)
    return " ".join(value.split())


def clarify(message, transcript):
    return {"status": "needs_clarification", "message": message, "transcript": transcript}


def resolve_assignee(user, name):
    candidates = User.objects.filter(is_active=True, department__isnull=False).select_related("department")
    if not (user.is_superuser or user.role in PRIVILEGED_TASK_ROLES or user.has_all_departments_access):
        candidates = candidates.filter(Q(department=user.department) | Q(department__users_with_access=user)).distinct()
    if name == "self":
        return user if user.department_id else None
    name = normalize(name)
    matches = [candidate for candidate in candidates if name in {
        normalize(candidate.email), normalize(candidate.get_full_name()),
        normalize(f"{candidate.last_name} {candidate.first_name}"),
        normalize(candidate.first_name), normalize(candidate.last_name),
    }]
    # Exact unique matches only: similar spellings and duplicate names require clarification.
    return matches[0] if len(matches) == 1 else None


def build_task(user, transcript):
    parsed = extract_task(transcript)
    if parsed.get("clarification"):
        return clarify(parsed["clarification"][:1000], transcript)
    extracted = ExtractedTaskSerializer(data=parsed)
    if not extracted.is_valid():
        return clarify("Please resend the complete request with a clear task, assignee and deadline.", transcript)
    data = extracted.validated_data
    assignee = resolve_assignee(user, data["assignee"])
    if not assignee:
        return clarify("The assignee could not be uniquely identified. Please resend the complete task with their full name or email.", transcript)
    if data["due_date"] and data["due_date"] < timezone.localdate():
        return clarify("The deadline is in the past. Please resend the complete task with a future date, including the year.", transcript)
    project = None
    if data["project"]:
        projects = Project.objects.filter(department=assignee.department).exclude(status=Project.Status.ARCHIVED)
        matches = [item for item in projects if normalize(item.name) == normalize(data["project"])]
        if len(matches) != 1:
            return clarify("The project could not be uniquely identified. Please resend the complete task with the exact project name.", transcript)
        project = matches[0]
    serializer = TaskSerializer(data={
        "title": data["title"], "description": data["description"],
        "assignees": [assignee.pk], "due_date": data["due_date"],
        "priority": data["priority"], "project": str(project.pk) if project else None,
    }, context={"request": SimpleNamespace(user=user)})
    serializer.is_valid(raise_exception=True)
    task = save_task(serializer, user)
    return {
        "status": "created", "transcript": transcript,
        "message": f"Task created: {task.title}\nAssigned to: {assignee.get_full_name() or assignee.email}\n"
                   f"Deadline: {task.due_date or 'Not specified'}",
        "task": {
            "id": str(task.pk), "title": task.title, "description": task.description,
            "assignees": [assignee.pk], "main_assignee": assignee.pk,
            "assignee_name": assignee.get_full_name(), "department": str(task.department_id),
            "project": str(task.project_id) if task.project_id else None,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "priority": task.priority, "status": task.status,
        },
    }


def create_ai_task(*, user, request_key, text="", audio=None, audio_loader=None, source_identity=""):
    if not user.is_active:
        raise PermissionDenied("Only active users can create tasks.")
    if audio is not None:
        validate_audio(audio)
        content = audio.read()
        audio.seek(0)
        fingerprint = hashlib.sha256(content).hexdigest()
    else:
        fingerprint = hashlib.sha256((text + "\0" + source_identity).encode()).hexdigest()
    # PostgreSQL row locking serializes retries across workers. Persist the result
    # in the same transaction as the task and its assignment notifications.
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        if not user.is_active:
            raise PermissionDenied("This account is inactive.")
        previous = AITaskRequest.objects.filter(user=user, request_key=request_key).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise serializers.ValidationError({"request_id": "This request ID has already been used for different content."})
            return previous.result
        if audio_loader is not None:
            audio = audio_loader()
        transcript = transcribe(audio) if audio is not None else text.strip()
        if not transcript or len(transcript) > 6000:
            raise serializers.ValidationError("The text must contain between 1 and 6000 characters.")
        result = build_task(user, transcript)
        AITaskRequest.objects.create(user=user, request_key=request_key, fingerprint=fingerprint, result=result)
        return result
