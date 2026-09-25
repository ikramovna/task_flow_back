"""Task creation policy shared by REST and the AI assistant."""
from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from .notifications import notify_task_assigned
from .task_visibility import PRIVILEGED_TASK_ROLES


def ensure_task_creator(user, department):
    privileged = user.is_superuser or user.role in PRIVILEGED_TASK_ROLES
    if not user.is_active or (not privileged and not user.can_access_department(department)):
        raise PermissionDenied("You can create tasks only in a department you can access.")


@transaction.atomic
def save_task(serializer, user):
    ensure_task_creator(user, serializer.validated_data["department"])
    task = serializer.save(created_by=user)
    notify_task_assigned(task, user, task.assignees.all())
    return task
