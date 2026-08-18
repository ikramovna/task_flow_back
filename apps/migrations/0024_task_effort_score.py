import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("apps", "0023_sync_task_completed_at")]

    operations = [
        migrations.AddField(
            model_name="task",
            name="effort_score",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Relative task complexity/effort from 1 (small) to 5 (very large).",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(5),
                ],
            ),
        ),
    ]
