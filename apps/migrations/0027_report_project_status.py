from django.db import migrations, models


def restore_project_status(apps, schema_editor):
    Report = apps.get_model("apps", "Report")
    Report.objects.filter(report_type="department_status").update(report_type="project_status")


def restore_department_status(apps, schema_editor):
    Report = apps.get_model("apps", "Report")
    Report.objects.filter(report_type="project_status").update(report_type="department_status")


class Migration(migrations.Migration):
    dependencies = [("apps", "0026_project_task_project")]

    operations = [
        migrations.RunPython(restore_project_status, restore_department_status),
        migrations.AlterField(
            model_name="report",
            name="report_type",
            field=models.CharField(
                choices=[
                    ("weekly_progress", "Weekly Progress"),
                    ("team_performance", "Team Performance"),
                    ("project_status", "Project Status"),
                    ("time_tracking", "Time Tracking"),
                    ("custom", "Custom"),
                ],
                max_length=24,
            ),
        ),
    ]
