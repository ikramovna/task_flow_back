import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0006_alter_timeentry_ended_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="department",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="projects",
                to="apps.department",
            ),
        ),
    ]
