from django.db.models import Q, QuerySet

from .models import Task, User


PRIVILEGED_TASK_ROLES = (User.Role.OWNER, User.Role.ADMIN, User.Role.MANAGER)


def visible_tasks_for(queryset: QuerySet[Task], user: User) -> QuerySet[Task]:
    """Hide private tasks from everyone except privileged roles and assignees."""
    if user.is_superuser or user.role in PRIVILEGED_TASK_ROLES:
        return queryset
    return queryset.filter(Q(is_hidden=False) | Q(assignees=user)).distinct()
