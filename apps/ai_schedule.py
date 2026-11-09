"""Create several tasks from a photo of a schedule: read → resolve names (asking when ambiguous) → confirm → create."""
import hashlib
import secrets
from datetime import date, timedelta
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException

from .ai_provider import extract_schedule
from .ai_tasks import clarify, exact_assignee_matches
from .models import AITaskRequest, User
from .serializers import TaskSerializer
from .task_creation import save_task

MAX_CHOICES = 8
PENDING_MINUTES = 30


def _person(user, name):
    matches = exact_assignee_matches(user, name)
    if len(matches) == 1:
        person = matches[0]
        return {"name": name, "status": "ok", "user_id": str(person.pk), "label": person.get_full_name() or person.email}
    if 1 < len(matches) <= MAX_CHOICES:
        options = sorted(matches, key=lambda person: (person.get_full_name(), person.email))
        return {"name": name, "status": "ambiguous", "candidates": [
            {"id": str(person.pk), "name": person.get_full_name() or person.email} for person in options]}
    return {"name": name, "status": "unknown"}


def build_schedule(*, user, request_key, image_loader, source_identity, note=""):
    """Idempotent per Telegram message: a replayed update returns the stored answer without a second AI call."""
    fingerprint = hashlib.sha256(f"schedule\0{source_identity}\0{note}".encode()).hexdigest()
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        previous = AITaskRequest.objects.filter(user=user, request_key=request_key).first()
        if previous:
            return previous.result
        image, mime_type = image_loader()
        parsed = extract_schedule(image, mime_type, note)
        today = timezone.localdate()
        items, skipped = [], 0
        for raw in parsed["tasks"]:
            try:
                day = date.fromisoformat(raw["date"])
            except ValueError:
                skipped += 1
                continue
            names = list(dict.fromkeys(raw["assignees"]))
            if day < today or not names:
                skipped += 1
                continue
            items.append({"title": raw["title"], "date": day.isoformat(), "time": raw["time"],
                          "people": [_person(user, name) for name in names]})
        if not items:
            result = clarify(parsed["clarification"] or "I found no upcoming events with people's names in this image.", "")
        else:
            result = {"status": "needs_confirmation", "action": "schedule",
                      "confirmation_code": secrets.token_hex(6).upper(), "items": items, "skipped": skipped,
                      "message": "Review the tasks found in the image."}
        AITaskRequest.objects.create(user=user, request_key=request_key, fingerprint=fingerprint, result=result)
        return result


def next_question(result):
    """The first person whose name matches several employees, as (item index, person index)."""
    for i, item in enumerate(result.get("items", [])):
        for j, person in enumerate(item["people"]):
            if person["status"] == "ambiguous":
                return i, j
    return None


def task_count(result):
    return sum(person["status"] == "ok" for item in result.get("items", []) for person in item["people"])


def _pending(user, code):
    return AITaskRequest.objects.select_for_update().filter(
        user=user, result__status="needs_confirmation", result__action="schedule",
        result__confirmation_code=code.upper(), created_at__gte=timezone.now() - timedelta(minutes=PENDING_MINUTES),
    ).order_by("-created_at").first()


EXPIRED = "That list expired or was already used. Send the image again."


@transaction.atomic
def choose_person(user, code, item_index, person_index, choice):
    """choice is a candidate index, or 's' to skip that person."""
    pending = _pending(user, code)
    if not pending:
        return clarify(EXPIRED, "")
    result = pending.result
    try:
        person = result["items"][item_index]["people"][person_index]
    except (IndexError, KeyError):
        return result
    if person["status"] == "ambiguous":
        if choice == "s":
            answer = {"status": "skipped"}
        else:
            try:
                picked = person["candidates"][int(choice)]
            except (ValueError, IndexError):
                return result
            answer = {"status": "ok", "user_id": picked["id"], "label": picked["name"]}
        # The same name means the same person everywhere in one schedule: ask once.
        for item in result["items"]:
            for other in item["people"]:
                if other["status"] == "ambiguous" and other["name"] == person["name"]:
                    other.update(answer)
        pending.result = result
        pending.save(update_fields=["result", "updated_at"])
    return result


@transaction.atomic
def confirm_schedule(user, code):
    pending = _pending(user, code)
    if not pending:
        return clarify(EXPIRED, "")
    result = pending.result
    if next_question(result):
        return result
    people = {str(person.pk): person for person in User.objects.filter(is_active=True, pk__in={
        person["user_id"] for item in result["items"] for person in item["people"] if person["status"] == "ok"})}
    created, failed = [], 0
    for item in result["items"]:
        for person in item["people"]:
            assignee = people.get(person.get("user_id")) if person["status"] == "ok" else None
            if not assignee:
                continue
            serializer = TaskSerializer(data={
                "title": item["title"], "description": f"Time: {item['time']}" if item["time"] else "",
                "assignees": [assignee.pk], "due_date": item["date"], "priority": "medium",
            }, context={"request": SimpleNamespace(user=user)})
            try:
                with transaction.atomic():
                    serializer.is_valid(raise_exception=True)
                    created.append(save_task(serializer, user))
            except (serializers.ValidationError, APIException):
                failed += 1  # e.g. an employee outside the departments this user may assign
    done = {"status": "created", "action": "schedule", "count": len(created), "failed": failed,
            "message": f"Created {len(created)} tasks.", "task_ids": [str(task.pk) for task in created]}
    pending.result = done
    pending.save(update_fields=["result", "updated_at"])
    return done
