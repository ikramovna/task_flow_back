import django.db.models.deletion
from django.db import migrations, models


def move_tasks_to_departments(apps, schema_editor):
    Department = apps.get_model("apps", "Department")
    Task = apps.get_model("apps", "Task")
    Report = apps.get_model("apps", "Report")

    fallback = Department.objects.order_by("created_at").first()
    if Task.objects.exists() and fallback is None:
        fallback = Department.objects.create(name="General", code="general")

    for task in Task.objects.select_related("project").iterator():
        department_id = task.project.department_id or fallback.id
        Task.objects.filter(pk=task.pk).update(department_id=department_id)

    Report.objects.filter(report_type="project_status").update(report_type="department_status")


class Migration(migrations.Migration):
    dependencies = [("apps", "0009_remove_workspace")]

    operations = [
        migrations.AddField(
            model_name="task",
            name="department",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="apps.department"),
        ),
        migrations.RunPython(move_tasks_to_departments, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="task",
            name="department",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="apps.department"),
        ),
        migrations.RemoveField(model_name="task", name="project"),
        migrations.AlterField(
            model_name="report",
            name="report_type",
            field=models.CharField(choices=[("weekly_progress", "Weekly Progress"), ("team_performance", "Team Performance"), ("department_status", "Department Status"), ("time_tracking", "Time Tracking"), ("custom", "Custom")], max_length=24),
        ),
        migrations.DeleteModel(name="Project"),
    ]
