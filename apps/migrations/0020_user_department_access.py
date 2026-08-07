from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("apps", "0019_task_main_assignee")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="accessible_departments",
            field=models.ManyToManyField(
                blank=True,
                help_text="Additional departments this user may access. The primary department is always included.",
                related_name="users_with_access",
                to="apps.department",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="has_all_departments_access",
            field=models.BooleanField(
                default=False,
                help_text="Allow access to every department, including departments created later.",
            ),
        ),
    ]
