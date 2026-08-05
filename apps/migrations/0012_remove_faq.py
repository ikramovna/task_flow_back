from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("apps", "0011_merge_membership_into_user")]

    operations = [migrations.DeleteModel(name="FAQ")]
