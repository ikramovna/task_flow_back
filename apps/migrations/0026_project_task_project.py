import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("apps", "0025_user_last_seen_and_presence")]

    operations = [
        migrations.CreateModel(
            name="Project",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=220)),
                ("description", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("planning", "Planning"), ("active", "Active"), ("on_hold", "On Hold"), ("completed", "Completed"), ("archived", "Archived")], default="planning", max_length=20)),
                ("priority", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="medium", max_length=10)),
                ("category", models.CharField(blank=True, max_length=80)),
                ("start_date", models.DateField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_projects", to="apps.user")),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="projects", to="apps.department")),
                ("manager", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_projects", to="apps.user")),
                ("team_members", models.ManyToManyField(blank=True, related_name="projects", to="apps.user")),
            ],
            options={
                "ordering": ("end_date", "-created_at"),
                "indexes": [
                    models.Index(fields=["department", "status"], name="project_dept_status_idx"),
                    models.Index(fields=["department", "end_date"], name="project_dept_end_idx"),
                ],
            },
        ),
        migrations.AddField(
            model_name="task",
            name="project",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tasks", to="apps.project"),
        ),
    ]
