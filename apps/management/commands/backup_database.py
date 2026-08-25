import os
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a PostgreSQL backup when the configured interval has elapsed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval-days",
            type=int,
            help="Override BACKUP_INTERVAL_DAYS for this run.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Create a backup even if the interval has not elapsed.",
        )

    def handle(self, *args, **options):
        interval_days = options["interval_days"]
        if interval_days is None:
            interval_days = settings.BACKUP_INTERVAL_DAYS
        if interval_days < 1:
            raise CommandError("Backup interval must be at least 1 day.")

        backup_dir = Path(settings.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        latest = self._latest_backup(backup_dir)
        now = timezone.now()

        if latest and not options["force"]:
            latest_time = timezone.datetime.fromtimestamp(
                latest.stat().st_mtime,
                tz=timezone.get_current_timezone(),
            )
            next_backup = latest_time + timedelta(days=interval_days)
            if now < next_backup:
                self.stdout.write(
                    self.style.WARNING(
                        f"Backup skipped. Next backup is due at {next_backup:%Y-%m-%d %H:%M:%S}."
                    )
                )
                return

        database = settings.DATABASES["default"]
        if database["ENGINE"] != "django.db.backends.postgresql":
            raise CommandError("backup_database currently supports PostgreSQL only.")

        filename = f"task_flow_{now:%Y%m%d_%H%M%S}.dump"
        destination = backup_dir / filename
        temporary = destination.with_suffix(".dump.tmp")
        command = [
            "pg_dump",
            "--format=custom",
            "--no-password",
            "--file",
            str(temporary),
            "--host",
            str(database.get("HOST") or "localhost"),
            "--port",
            str(database.get("PORT") or "5432"),
            "--username",
            str(database.get("USER") or ""),
            str(database["NAME"]),
        ]
        environment = os.environ.copy()
        if database.get("PASSWORD"):
            environment["PGPASSWORD"] = str(database["PASSWORD"])

        try:
            subprocess.run(command, check=True, env=environment)
            temporary.replace(destination)
        except FileNotFoundError as exc:
            temporary.unlink(missing_ok=True)
            raise CommandError("pg_dump was not found. Install PostgreSQL client tools.") from exc
        except subprocess.CalledProcessError as exc:
            temporary.unlink(missing_ok=True)
            raise CommandError(f"pg_dump failed with exit code {exc.returncode}.") from exc

        self.stdout.write(self.style.SUCCESS(f"Backup created: {destination}"))

    @staticmethod
    def _latest_backup(backup_dir):
        backups = backup_dir.glob("task_flow_*.dump")
        return max(backups, key=lambda path: path.stat().st_mtime, default=None)
