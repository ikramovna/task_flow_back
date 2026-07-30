from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0005_alter_event_event_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="timeentry",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
