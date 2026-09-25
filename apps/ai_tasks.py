"""Shared text/voice task workflow for Tiko and Telegram."""
import hashlib
import re
import secrets
import unicodedata
import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from .ai_provider import extract_task, extract_task_change, transcribe, validate_audio
from .models import AITaskRequest, Project, Task, User
from .notifications import notify_task_assigned, notify_task_completed
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


DELETE_WORDS = re.compile(r"\b(?:ochir\w*|delete\w*|remove\w*|удал\w*)\b")
UPDATE_WORDS = re.compile(
    r"\b(?:edit\w*|update\w*|modify\w*|change\w*|tahrir\w*|ozgartir\w*|"
    r"togrila\w*|tugrila\w*|togla\w*|tuzat\w*|almashtir\w*|редактир\w*|измен\w*|исправ\w*)\b"
)
TASK_REFERENCE = re.compile(
    r"\b(?:task\w*|vazifa\w*|задач\w*|last|latest|oldingi|oxirgi|hozirgi|"
    r"yaratgan\w*|created|shu|uni|buni|it|this|that)\b"
)
DELETE_CONFIRMATION = re.compile(r"^confirm delete ([0-9a-f]{12})$", re.IGNORECASE)


def task_intent(text):
    normalized = normalize(text)
    if DELETE_CONFIRMATION.fullmatch(normalized):
        return "confirm_delete"
    if re.match(r"^(?:create (?:a )?task|task yarat\w*|vazifa yarat\w*|созда\w* задач\w*)\b", normalized):
        return "create"
    has_target = bool(TASK_REFERENCE.search(normalized))
    delete = has_target and bool(DELETE_WORDS.search(normalized))
    update = has_target and bool(UPDATE_WORDS.search(normalized))
    if delete and update:
        return "ambiguous"
    return "delete" if delete else "update" if update else "create"


def latest_ai_task(user):
    receipt = AITaskRequest.objects.filter(user=user, result__status="created").order_by("-created_at").first()
    task_id = (receipt.result.get("task") or {}).get("id") if receipt else None
    return Task.objects.filter(pk=task_id, created_by=user, is_archived=False).first() if task_id else None


def resolve_task_target(user, target):
    target = (target or "").strip()
    if normalize(target) in {"last", "latest", "recent", "current"}:
        return latest_ai_task(user)
    if target:
        try:
            task_id = uuid.UUID(target)
        except ValueError:
            matches = [task for task in Task.objects.filter(created_by=user, is_archived=False)
                       if normalize(task.title) == normalize(target)]
            return matches[0] if len(matches) == 1 else None
        return Task.objects.filter(pk=task_id, created_by=user, is_archived=False).first()
    recent = Task.objects.filter(created_by=user, is_archived=False,
                                 created_at__gte=timezone.now() - timedelta(minutes=30))
    return recent.first() if recent.count() == 1 else None


def one_edit_apart(left, right):
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    index = 0
    edits = 0
    for char in right:
        if index < len(left) and left[index] == char:
            index += 1
        else:
            edits += 1
            if edits > 1:
                return False
            if len(left) == len(right):
                index += 1
    return edits == 1


def resolve_assignee(user, name, *, from_voice=False):
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
    if len(matches) == 1:
        return matches[0]
    if matches or not from_voice:
        return None
    # Speech recognition can change one letter of a name. Only recover a
    # uniquely matching full name when the other name part is exact.
    parts = name.split()
    if len(parts) != 2 or len(parts[0]) < 5 or len(parts[1]) < 6:
        return None
    near_matches = [candidate for candidate in candidates
                    if (normalize(candidate.first_name) == parts[0]
                        and one_edit_apart(parts[1], normalize(candidate.last_name)))
                    or (normalize(candidate.last_name) == parts[1]
                        and one_edit_apart(parts[0], normalize(candidate.first_name)))]
    return near_matches[0] if len(near_matches) == 1 else None


def build_task(user, transcript, *, from_voice=False):
    parsed = extract_task(transcript)
    if parsed.get("clarification"):
        return clarify(parsed["clarification"][:1000], transcript)
    extracted = ExtractedTaskSerializer(data=parsed)
    if not extracted.is_valid():
        return clarify("Please resend the complete request with a clear task, assignee and deadline.", transcript)
    data = extracted.validated_data
    assignee = resolve_assignee(user, data["assignee"], from_voice=from_voice)
    if not assignee:
        return clarify(f"The assignee was read as '{data['assignee'][:120]}' but could not be uniquely identified. "
                       "Please resend the complete task with their full name or email.", transcript)
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


def ensure_ai_task_manager(user, task):
    if not user.is_active or user.role not in PRIVILEGED_TASK_ROLES or task.created_by_id != user.pk:
        raise PermissionDenied("You can manage only your own tasks as an Owner, Admin, or Manager.")


def build_task_change(user, transcript, intent, *, from_voice=False):
    parsed = extract_task_change(transcript)
    if parsed["action"] != intent or parsed["clarification"]:
        return clarify(parsed["clarification"] or "Please make one clear edit or delete request.", transcript)
    task = resolve_task_target(user, parsed["target"])
    if not task:
        return clarify("I could not identify one task. Say 'my last created task' or give the exact task title.", transcript)
    task = Task.objects.select_for_update().filter(pk=task.pk, is_archived=False).first()
    if not task:
        return clarify("That task is no longer available.", transcript)
    ensure_ai_task_manager(user, task)
    if intent == "delete":
        code = secrets.token_hex(6).upper()
        return {
            "status": "needs_confirmation", "action": "delete", "confirmation_code": code,
            "transcript": transcript, "task": {"id": str(task.pk), "title": task.title},
            "message": f"Delete '{task.title}'? Send CONFIRM DELETE {code} within 10 minutes to permanently delete it.",
        }

    changes = {key: parsed[key] for key in ("title", "description", "due_date", "priority", "status")
               if parsed[key] is not None}
    if "due_date" in changes:
        try:
            due_date = serializers.DateField().run_validation(changes["due_date"])
        except serializers.ValidationError:
            return clarify("Give the new deadline as an exact day, month, and year.", transcript)
        if due_date < timezone.localdate():
            return clarify("The new deadline is in the past. Give a future date.", transcript)
    if parsed["assignee"]:
        assignee = resolve_assignee(user, parsed["assignee"], from_voice=from_voice)
        if not assignee:
            return clarify("The new assignee could not be uniquely identified. Use their full name or email.", transcript)
        changes["assignees"] = [assignee.pk]
    if parsed["project"]:
        department = assignee.department if "assignees" in changes else task.department
        projects = Project.objects.filter(department=department).exclude(status=Project.Status.ARCHIVED)
        matches = [project for project in projects if normalize(project.name) == normalize(parsed["project"])]
        if len(matches) != 1:
            return clarify("The new project could not be uniquely identified. Use its exact name.", transcript)
        changes["project"] = matches[0].pk
    if not changes:
        return clarify("Say what to change in that task, such as its title, deadline, or priority.", transcript)
    if "status" in changes and changes["status"] != task.status and task.main_assignee_id != user.pk:
        raise PermissionDenied("Only the main assignee can change task status.")
    previous_status = task.status
    previous_assignees = set(task.assignees.values_list("pk", flat=True))
    serializer = TaskSerializer(task, data=changes, partial=True, context={"request": SimpleNamespace(user=user)})
    serializer.is_valid(raise_exception=True)
    task = serializer.save()
    if "assignees" in changes:
        notify_task_assigned(task, user, task.assignees.exclude(pk__in=previous_assignees))
    if task.status == Task.Status.COMPLETED and previous_status != Task.Status.COMPLETED:
        task.progress, task.completed_at = 100, timezone.now()
        task.save(update_fields=["progress", "completed_at", "updated_at"])
        notify_task_completed(task, user)
    elif task.status != Task.Status.COMPLETED and previous_status == Task.Status.COMPLETED:
        task.completed_at = None
        task.save(update_fields=["completed_at", "updated_at"])
    return {
        "status": "updated", "action": "update", "transcript": transcript,
        "message": f"Task updated: {task.title}",
        "task": {"id": str(task.pk), "title": task.title, "description": task.description,
                 "due_date": task.due_date.isoformat() if task.due_date else None,
                 "priority": task.priority, "status": task.status,
                 "assignee_name": task.main_assignee.get_full_name() if task.main_assignee else ""},
    }


@transaction.atomic
def confirm_ai_delete(user, code):
    pending = AITaskRequest.objects.select_for_update().filter(
        user=user, result__status="needs_confirmation", result__confirmation_code=code.upper(),
        created_at__gte=timezone.now() - timedelta(minutes=10),
    ).order_by("-created_at").first()
    if not pending:
        return clarify("That delete confirmation expired or was already used. Send a new delete request.", "")
    task_id = (pending.result.get("task") or {}).get("id")
    task = Task.objects.select_for_update().filter(pk=task_id, created_by=user, is_archived=False).first()
    if not task:
        return clarify("That task is no longer available.", "")
    ensure_ai_task_manager(user, task)
    title = task.title
    task.delete()
    result = {"status": "deleted", "action": "delete", "message": f"Task deleted: {title}",
              "task": {"id": str(task_id), "title": title}}
    pending.result = result
    pending.save(update_fields=["result", "updated_at"])
    return result


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
        intent = task_intent(transcript)
        if intent == "confirm_delete":
            code = DELETE_CONFIRMATION.fullmatch(normalize(transcript)).group(1)
            result = confirm_ai_delete(user, code)
        elif intent == "ambiguous":
            result = clarify("Please request either an edit or a deletion, one at a time.", transcript)
        elif intent == "create":
            result = build_task(user, transcript, from_voice=audio is not None)
        else:
            result = build_task_change(user, transcript, intent, from_voice=audio is not None)
        if audio is not None and result["status"] in {"needs_clarification", "needs_confirmation"}:
            result["message"] += f"\n\nI heard: {transcript[:600]}"
        AITaskRequest.objects.create(user=user, request_key=request_key, fingerprint=fingerprint, result=result)
        return result
