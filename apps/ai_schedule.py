"""Calendar events from a schedule photo or a meeting request: read → resolve names (ask when ambiguous) → confirm → add."""
import hashlib
import re
import secrets
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException

from .ai_provider import extract_event, extract_schedule
from .ai_tasks import (ALL_REFERENCE, assignee_names, clarify, confident_match, exact_assignee_matches,
                       find_project, is_task_manager, mentions_self, normalize, project_note, resolve_assignee,
                       similar_assignees, team_members)
from .models import AITaskRequest, Notification, User
from .notifications import create_notification
from .serializers import EventSerializer, TaskSerializer
from .task_creation import save_task

MAX_CHOICES = 8
PENDING_MINUTES = 30
DEFAULT_START = time(9, 0)
DEFAULT_LENGTH = timedelta(hours=1)
EXPIRED = "That list expired or was already used. Send it again."
ALL_NAMES = {"all", "everyone", "all staff", "all employees", "hammaga", "barchaga", "всем"}


def _ok(person):
    return {"name": person.get_full_name() or person.email, "status": "ok", "user_id": str(person.pk),
            "label": person.get_full_name() or person.email}


def _person(user, name):
    matches = exact_assignee_matches(user, name)
    if len(matches) == 1:
        return {**_ok(matches[0]), "name": name}
    if 1 < len(matches) <= MAX_CHOICES:
        options = sorted(matches, key=lambda person: (person.get_full_name(), person.email))
        return {"name": name, "status": "ambiguous", "candidates": [
            {"id": str(person.pk), "name": person.get_full_name() or person.email} for person in options]}
    if not matches:
        # No exact spelling: take one clearly closest employee, or offer the closest ones as buttons.
        suggestions = similar_assignees(user, name)
        match = confident_match(suggestions)
        if match:
            return {**_ok(match), "name": name}
        if suggestions:
            return {"name": name, "status": "ambiguous", "candidates": [
                {"id": str(person.pk), "name": person.get_full_name() or person.email} for _, person in suggestions]}
    return {"name": name, "status": "unknown"}


def _people(user, names, transcript=""):
    """'all' and 'self' are honoured only when the request itself says so."""
    people = []
    for name in names:
        key = normalize(name)
        if key in ALL_NAMES:
            if transcript and ALL_REFERENCE.search(transcript) and is_task_manager(user):
                people += [_ok(member) for member in team_members(user)]
            else:
                people.append({"name": name, "status": "unknown"})
        elif key == "self":
            if mentions_self(transcript):
                people.append(_ok(user))
        else:
            people.append(_person(user, name))
    unique, seen = [], set()
    for person in people:
        marker = person.get("user_id") or f"name:{person['name']}"
        if marker not in seen:
            seen.add(marker)
            unique.append(person)
    return unique


def _clock(value):
    if value and re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
        hours, minutes = value.split(":")
        return f"{int(hours):02d}:{minutes}"
    return None


def _item(user, raw, transcript=""):
    try:
        day = date.fromisoformat(raw["date"])
    except ValueError:
        return None
    return {"title": raw["title"], "date": day.isoformat(), "time": _clock(raw["time"]),
            "end_time": _clock(raw["end_time"]), "location": raw["location"],
            "people": _people(user, raw["attendees"], transcript)}


def _pending_result(items, *, kind, skipped=0):
    code = secrets.token_hex(6).upper()
    return {"status": "needs_confirmation", "action": "schedule", "kind": kind, "confirmation_code": code,
            "items": items, "skipped": skipped,
            "message": f"Add {len(items)} event(s) to the calendar? Send CONFIRM EVENT {code} to add."}


def build_schedule(*, user, request_key, image_loader, source_identity, note=""):
    """A calendar photo: idempotent per Telegram message, so a replayed update never reads the image twice."""
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
        for raw in parsed["events"]:
            item = _item(user, raw)
            if not item or item["date"] < today.isoformat():
                skipped += 1
                continue
            items.append(item)
        if items:
            result = _pending_result(items, kind="schedule", skipped=skipped)
        else:
            result = clarify(parsed["clarification"] or "I found no upcoming events in this image.", "")
        AITaskRequest.objects.create(user=user, request_key=request_key, fingerprint=fingerprint, result=result)
        return result


def build_meeting(user, transcript):
    """A meeting asked for by text or voice; the caller stores the receipt."""
    parsed = extract_event(transcript, names=assignee_names(user, limit=120))
    if parsed["clarification"] or not parsed["events"]:
        return clarify(parsed["clarification"] or "Please send the meeting with its day, time and people.", transcript)
    item = _item(user, parsed["events"][0], transcript)
    if not item:
        return clarify("Please give the meeting date as day, month and year.", transcript)
    if not item["time"]:
        return clarify("What time is the meeting? Please send it again with the time.", transcript)
    if item["date"] < timezone.localdate().isoformat():
        return clarify("That date is in the past. Please send a future date.", transcript)
    result = _pending_result([item], kind="meeting")
    result["transcript"] = transcript
    return result


def build_multi_task(user, data, names, transcript, *, from_voice=False):
    """One piece of work for 2+ named people: each gets their own task (like "all employees").
    Created at once when every name is clear; otherwise asked and confirmed like a meeting."""
    people = _people(user, names, transcript)
    if from_voice:
        for index, person in enumerate(people):
            if person["status"] == "unknown":
                match = resolve_assignee(user, person["name"], from_voice=True)
                if match:
                    people[index] = {**_ok(match), "name": person["name"]}
    item = {"title": data["title"], "date": data["due_date"].isoformat() if data["due_date"] else None,
            "time": None, "end_time": None, "location": None, "people": people,
            "description": data["description"], "priority": data["priority"], "project": data["project"]}
    if people and all(person["status"] == "ok" for person in people):
        return create_tasks(user, item)
    result = _pending_result([item], kind="tasks")
    result["message"] = (f"Create “{data['title'][:120]}” for {len(people)} people? "
                         f"Send CONFIRM TASKS {result['confirmation_code']} to create.")
    result["transcript"] = transcript
    return result


def create_tasks(user, item):
    """One task per selected person; people outside the departments this user may assign are skipped."""
    ids = [person["user_id"] for person in item["people"] if person["status"] == "ok"]
    found = {str(person.pk): person for person in User.objects.filter(is_active=True, pk__in=ids)}
    projects, tasks, failed = {}, [], 0
    for uid in dict.fromkeys(ids):
        assignee = found.get(uid)
        if not assignee:
            failed += 1
            continue
        if item["project"] and assignee.department_id not in projects:
            projects[assignee.department_id] = find_project(assignee.department_id, item["project"])
        project = projects.get(assignee.department_id)
        serializer = TaskSerializer(data={
            "title": item["title"], "description": item["description"], "assignees": [assignee.pk],
            "due_date": item["date"], "priority": item["priority"], "project": str(project.pk) if project else None,
        }, context={"request": SimpleNamespace(user=user)})
        try:
            with transaction.atomic():
                serializer.is_valid(raise_exception=True)
                tasks.append((save_task(serializer, user), assignee))
        except (serializers.ValidationError, APIException):
            failed += 1
    if not tasks:
        return clarify("No task could be created for these people. Check their departments.", "")
    names = [assignee.get_full_name() or assignee.email for _, assignee in tasks]
    first = tasks[0][0]
    return {
        "status": "created", "action": "multi", "count": len(tasks), "failed": failed,
        "message": f"Created {len(tasks)} tasks: {first.title}\nAssigned to: {', '.join(names)}",
        "task": {"id": str(first.pk), "title": first.title, "assignee_name": ", ".join(names),
                 "due_date": first.due_date.isoformat() if first.due_date else None,
                 "priority": first.priority, "status": first.status},
        "task_ids": [str(task.pk) for task, _ in tasks],
        "project_note": project_note(item["project"], next((p for p in projects.values() if p), None)),
    }


def next_question(result):
    """The first person whose name matches several employees, as (item index, person index)."""
    for i, item in enumerate(result.get("items", [])):
        for j, person in enumerate(item["people"]):
            if person["status"] == "ambiguous":
                return i, j
    return None


def _pending(user, code):
    return AITaskRequest.objects.select_for_update().filter(
        user=user, result__status="needs_confirmation", result__action="schedule",
        result__confirmation_code=code.upper(), created_at__gte=timezone.now() - timedelta(minutes=PENDING_MINUTES),
    ).order_by("-created_at").first()


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
        # The same name means the same person everywhere in one list: ask once.
        for item in result["items"]:
            for other in item["people"]:
                if other["status"] == "ambiguous" and other["name"] == person["name"]:
                    other.update(answer)
        pending.result = result
        pending.save(update_fields=["result", "updated_at"])
    return result


MAX_TOGGLES = 30


def people_order(result):
    """Everyone found, once, in order of first appearance: (user id, label, selected)."""
    seen = {}
    for item in result.get("items", []):
        for person in item["people"]:
            if person["status"] in {"ok", "removed"}:
                uid = person["user_id"]
                selected = seen.get(uid, (None, False))[1] or person["status"] == "ok"
                seen[uid] = (person["label"], selected)
    return [(uid, label, selected) for uid, (label, selected) in seen.items()]


@transaction.atomic
def toggle_person(user, code, index):
    """Untick (or tick again) one person in every event of the pending list."""
    pending = _pending(user, code)
    if not pending:
        return clarify(EXPIRED, "")
    result = pending.result
    order = people_order(result)
    if index >= len(order):
        return result
    uid, _, selected = order[index]
    for item in result["items"]:
        for person in item["people"]:
            if person.get("user_id") == uid and person["status"] in {"ok", "removed"}:
                person["status"] = "removed" if selected else "ok"
    pending.result = result
    pending.save(update_fields=["result", "updated_at"])
    return result


def _times(item):
    day = date.fromisoformat(item["date"])
    start_clock = time.fromisoformat(item["time"]) if item["time"] else DEFAULT_START
    starts = timezone.make_aware(datetime.combine(day, start_clock))
    ends = timezone.make_aware(datetime.combine(day, time.fromisoformat(item["end_time"]))) if item["end_time"] else None
    if not ends or ends <= starts:
        ends = starts + DEFAULT_LENGTH
    return starts, ends


def when_text(starts, ends):
    starts, ends = timezone.localtime(starts), timezone.localtime(ends)
    return f"{starts:%a %d %b} · {starts:%H:%M}–{ends:%H:%M}"


@transaction.atomic
def confirm_schedule(user, code):
    pending = _pending(user, code)
    if not pending:
        return clarify(EXPIRED, "")
    result = pending.result
    if next_question(result):
        return result
    if result.get("kind") == "tasks":
        done = create_tasks(user, result["items"][0])
        pending.result = done
        pending.save(update_fields=["result", "updated_at"])
        return done
    ids = {person["user_id"] for item in result["items"] for person in item["people"] if person["status"] == "ok"}
    people = {str(person.pk): person for person in User.objects.filter(is_active=True, pk__in=ids)}
    created, failed, invited = [], 0, 0
    for item in result["items"]:
        attendees = [people[p["user_id"]] for p in item["people"] if p["status"] == "ok" and p["user_id"] in people]
        department = user.department or (attendees[0].department if attendees else None)
        if department is None:
            failed += 1
            continue
        # Like the web calendar: attendees from departments this user cannot access are left out.
        attendees = [a for a in attendees if a.department_id == department.id or user.can_access_department(a.department_id)]
        starts, ends = _times(item)
        serializer = EventSerializer(data={
            "department": department.pk, "title": item["title"][:180], "event_type": "meeting",
            "starts_at": starts, "ends_at": ends, "location": (item["location"] or "")[:250],
            "description": "Time not given on the calendar." if not item["time"] else "",
            "attendees": [attendee.pk for attendee in attendees],
        }, context={"request": SimpleNamespace(user=user)})
        try:
            with transaction.atomic():
                if not user.can_access_department(department):
                    raise serializers.ValidationError("No access to this department.")
                serializer.is_valid(raise_exception=True)
                event = serializer.save(created_by=user)
        except (serializers.ValidationError, APIException):
            failed += 1
            continue
        created.append(event)
        body = when_text(starts, ends) + (f"\n📍 {item['location']}" if item["location"] else "")
        body += f"\nfrom {user.get_full_name() or user.email}"
        for attendee in attendees:
            if attendee.pk != user.pk:
                create_notification(recipient=attendee, actor=user, notification_type=Notification.Type.EVENT_INVITE,
                                     title=f"New meeting: {event.title}"[:180], body=body)
                invited += 1
    done = {"status": "created", "action": "schedule", "kind": result.get("kind", "schedule"),
            "count": len(created), "failed": failed, "invited": invited,
            "message": f"Added {len(created)} event(s) to the calendar.",
            "event_ids": [str(event.pk) for event in created]}
    if len(created) == 1:
        done["event"] = {"title": created[0].title, "when": when_text(created[0].starts_at, created[0].ends_at)}
    pending.result = done
    pending.save(update_fields=["result", "updated_at"])
    return done
