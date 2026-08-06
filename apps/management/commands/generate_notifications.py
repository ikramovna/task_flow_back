from django.core.management.base import BaseCommand

from apps.notifications import generate_deadline_notifications


class Command(BaseCommand):
    help = "Generate idempotent deadline and overdue notifications"

    def handle(self, *args, **options):
        created = generate_deadline_notifications()
        self.stdout.write(self.style.SUCCESS(f"Created {created} notification(s)."))
