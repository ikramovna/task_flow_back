import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0020_user_department_access"),
    ]

    operations = [
        migrations.CreateModel(
            name="TelegramIntegration",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("telegram_user_id", models.BigIntegerField(blank=True, null=True, unique=True)),
                ("telegram_chat_id", models.BigIntegerField(blank=True, null=True, unique=True)),
                ("telegram_username", models.CharField(blank=True, max_length=255)),
                ("link_token", models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ("link_token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("is_connected", models.BooleanField(default=False)),
                ("notifications_enabled", models.BooleanField(default=True)),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="telegram_integration", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
