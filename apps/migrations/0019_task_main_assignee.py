from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_main_assignees(apps, schema_editor):
    Task = apps.get_model("apps", "Task")
    through = Task.assignees.through
    for task in Task.objects.iterator():
        first_link = through.objects.filter(task_id=task.pk).order_by("pk").first()
        if first_link:
            task.main_assignee_id = first_link.user_id
            task.save(update_fields=("main_assignee",))


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0018_task_is_hidden"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="main_assignee",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="main_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_main_assignees, migrations.RunPython.noop),
    ]
