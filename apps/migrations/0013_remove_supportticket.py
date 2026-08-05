from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("apps", "0012_remove_faq")]

    operations = [migrations.DeleteModel(name="SupportTicket")]
