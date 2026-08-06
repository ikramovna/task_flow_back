from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("apps", "0017_event_event_dept_starts_idx_and_more")]

    operations = [
        migrations.AddField(
            model_name="task",
            name="is_hidden",
            field=models.BooleanField(default=False),
        ),
    ]
