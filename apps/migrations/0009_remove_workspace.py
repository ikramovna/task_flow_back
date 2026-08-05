import django.db.models.deletion
from django.db import migrations, models


def copy_workspace_scope_to_departments(apps, schema_editor):
    Department = apps.get_model("apps", "Department")
    Event = apps.get_model("apps", "Event")
    Conversation = apps.get_model("apps", "Conversation")
    Report = apps.get_model("apps", "Report")

    first_department_by_workspace = dict(
        Department.objects.order_by("created_at").values_list("workspace_id", "id")
    )
    for model in (Event, Conversation, Report):
        for item in model.objects.filter(department__isnull=True).iterator():
            department_id = first_department_by_workspace.get(item.workspace_id)
            if department_id:
                model.objects.filter(pk=item.pk).update(department_id=department_id)


class Migration(migrations.Migration):
    dependencies = [("apps", "0008_backfill_project_departments")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="department",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="events", to="apps.department"),
        ),
        migrations.AddField(
            model_name="conversation",
            name="department",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="conversations", to="apps.department"),
        ),
        migrations.AddField(
            model_name="report",
            name="department",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reports", to="apps.department"),
        ),
        migrations.RunPython(copy_workspace_scope_to_departments, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="department", name="unique_department_name_per_workspace"),
        migrations.RemoveConstraint(model_name="department", name="unique_department_code_per_workspace"),
        migrations.RemoveConstraint(model_name="membership", name="unique_workspace_member"),
        migrations.RemoveField(model_name="event", name="workspace"),
        migrations.RemoveField(model_name="conversation", name="workspace"),
        migrations.RemoveField(model_name="report", name="workspace"),
        migrations.RemoveField(model_name="project", name="workspace"),
        migrations.RemoveField(model_name="membership", name="workspace"),
        migrations.RemoveField(model_name="department", name="workspace"),
        migrations.AlterField(model_name="department", name="code", field=models.SlugField(max_length=32, unique=True)),
        migrations.AddConstraint(model_name="department", constraint=models.UniqueConstraint(fields=("name",), name="unique_department_name")),
        migrations.AddConstraint(model_name="membership", constraint=models.UniqueConstraint(fields=("user",), name="unique_organization_member")),
        migrations.DeleteModel(name="Workspace"),
    ]
