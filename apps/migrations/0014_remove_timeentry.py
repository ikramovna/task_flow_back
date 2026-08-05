from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("apps", "0013_remove_supportticket")]

    operations = [migrations.DeleteModel(name="TimeEntry")]
