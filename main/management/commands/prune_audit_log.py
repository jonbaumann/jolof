"""Apply the audit log's retention policy.

The audit log page prunes itself opportunistically, at most hourly, so this is
here for an installation that would rather do it from cron -- or wants to see
what a retention change would remove before making it.
"""
from django.core.management.base import BaseCommand

from main.models import AuditLog, AuditSettings


class Command(BaseCommand):
    help = "Delete audit log entries older than the configured retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        days = AuditSettings.load().retention_days
        if not days:
            self.stdout.write("Retention is set to keep everything forever; nothing to do.")
            return

        cutoff = AuditLog.retention_cutoff()
        if options['dry_run']:
            count = AuditLog.objects.filter(timestamp__lt=cutoff).count()
            self.stdout.write(
                f"Would delete {count} entr{'y' if count == 1 else 'ies'} "
                f"older than {days} days (before {cutoff:%Y-%m-%d %H:%M} UTC)."
            )
            return

        deleted = AuditLog.prune()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted} entr{'y' if deleted == 1 else 'ies'} older than {days} days."
        ))
