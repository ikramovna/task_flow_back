import django_filters

from .models import Event, Task


class TaskFilter(django_filters.FilterSet):
    due_from = django_filters.DateFilter(field_name="due_date", lookup_expr="gte")
    due_to = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")

    class Meta:
        model = Task
        fields = ("department", "status", "priority", "category", "assignees")


class EventFilter(django_filters.FilterSet):
    starts_from = django_filters.IsoDateTimeFilter(field_name="starts_at", lookup_expr="gte")
    starts_to = django_filters.IsoDateTimeFilter(field_name="starts_at", lookup_expr="lte")

    class Meta:
        model = Event
        fields = ("event_type", "attendees")
