from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0004_department_membership_department"),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="event_type",
            field=models.CharField(default="meeting", max_length=80),
        ),
    ]
