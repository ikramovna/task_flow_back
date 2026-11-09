from django.db import migrations, models


def enable_deadline_reminders(apps, schema_editor):
    # The old default was off, so almost nobody chose it explicitly: turn it on for everyone once.
    apps.get_model("apps", "UserPreference").objects.update(deadline_reminder=True)


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0030_digest_notifications'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userpreference',
            name='deadline_reminder',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(enable_deadline_reminders, migrations.RunPython.noop),
    ]
