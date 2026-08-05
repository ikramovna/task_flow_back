import django.db.models.deletion
from django.db import migrations, models


def copy_membership_to_user(apps, schema_editor):
    User = apps.get_model("apps", "User")
    Membership = apps.get_model("apps", "Membership")

    for membership in Membership.objects.all().iterator():
        User.objects.filter(pk=membership.user_id).update(
            department_id=membership.department_id,
            role=membership.role,
            is_active=membership.is_active,
        )


class Migration(migrations.Migration):
    dependencies = [("apps", "0010_remove_project")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="department",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="users", to="apps.department"),
        ),
        migrations.AddField(
            model_name="user",
            name="role",
            field=models.CharField(choices=[("owner", "Owner"), ("admin", "Admin"), ("manager", "Manager"), ("member", "Member")], default="member", max_length=16),
        ),
        migrations.RunPython(copy_membership_to_user, migrations.RunPython.noop),
        migrations.DeleteModel(name="Membership"),
    ]
