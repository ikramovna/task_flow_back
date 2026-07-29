import django_filters

from .models import Event, Project, Task


class TaskFilter(django_filters.FilterSet):
    due_from = django_filters.DateFilter(field_name="due_date", lookup_expr="gte")
    due_to = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")

    class Meta:
        model = Task
        fields = ("project", "status", "priority", "category", "assignees")


class ProjectFilter(django_filters.FilterSet):
    class Meta:
        model = Project
        fields = ("status", "priority", "members")


class EventFilter(django_filters.FilterSet):
    starts_from = django_filters.IsoDateTimeFilter(field_name="starts_at", lookup_expr="gte")
    starts_to = django_filters.IsoDateTimeFilter(field_name="starts_at", lookup_expr="lte")

    class Meta:
        model = Event
        fields = ("event_type", "attendees")

