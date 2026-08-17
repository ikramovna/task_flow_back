from django.db import migrations
from django.db.models import F


def sync_task_completion_fields(apps, schema_editor):
    Task = apps.get_model("apps", "Task")
    Task.objects.filter(status="completed", completed_at__isnull=True).update(
        completed_at=F("updated_at"),
        progress=100,
    )
    Task.objects.exclude(status="completed").filter(completed_at__isnull=False).update(
        completed_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [("apps", "0022_normalize_legacy_task_statuses")]

    operations = [migrations.RunPython(sync_task_completion_fields, migrations.RunPython.noop)]
