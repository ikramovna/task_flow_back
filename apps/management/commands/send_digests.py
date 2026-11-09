from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.digests import send_daily_digests, send_weekly_performance
from apps.notifications import generate_deadline_notifications


class Command(BaseCommand):
    help = ("Send push notifications: deadline/overdue reminders and the daily plan; "
            "on Mondays (or with --weekly) also the weekly performance summary. Safe to rerun.")

    def add_arguments(self, parser):
        parser.add_argument("--weekly", action="store_true", help="Send the weekly summary even if today is not Monday.")

    def handle(self, *args, **options):
        today = timezone.localdate()
        deadlines = generate_deadline_notifications(today)
        daily = send_daily_digests(today)
        weekly = send_weekly_performance(today) if options["weekly"] or today.weekday() == 0 else 0
        self.stdout.write(self.style.SUCCESS(
            f"Deadline reminders: {deadlines} · Daily plans: {daily} · Weekly summaries: {weekly}"
        ))
