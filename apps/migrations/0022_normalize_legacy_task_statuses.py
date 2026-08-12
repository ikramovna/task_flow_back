from django.db import migrations
from django.utils import timezone


def normalize_legacy_task_statuses(apps, schema_editor):
    Task = apps.get_model("apps", "Task")

    Task.objects.filter(status="at_risk").update(status="in_progress")

    archived_tasks = Task.objects.filter(status="archived")
    archived_tasks.update(
        status="completed",
        is_archived=True,
        archived_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0021_telegramintegration"),
    ]

    operations = [
        migrations.RunPython(normalize_legacy_task_statuses, migrations.RunPython.noop),
    ]
