from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mrp import models, reminders, services


class Command(BaseCommand):
    help = ('Email a reminder for each vendor whose order-by date is within the reminder window (run daily from cron). '
            'With --weekly, also send the Monday digest to users who subscribed.')

    def add_arguments(self, parser):
        parser.add_argument('--weekly', action='store_true', help='Send the weekly digest to subscribed users.')
        parser.add_argument('--dry-run', action='store_true', help='Print what would be sent; send nothing.')
        parser.add_argument('--force', action='store_true', help='Ignore the 7-day duplicate check.')

    def handle(self, *args, weekly=False, dry_run=False, force=False, **options):
        if not models.SyncRun.objects.filter(status=models.SyncRun.OK).exists():
            raise CommandError('No planning data loaded yet; nothing to remind about.')
        result = services.build_plan(services.default_options())

        outgoing = []
        if settings.MRP_REMINDER_TO:
            outgoing += reminders.vendor_reminder_emails(result, settings.MRP_REMINDER_TO, force=force)
        else:
            self.stderr.write('MRP_REMINDER_TO is not set; skipping vendor reminders.')
        if weekly:
            emails = sorted({s.user.email for s in models.WeeklyDigestSubscription.objects.select_related('user')
                             if s.user.email and s.user.is_active})
            for email in emails:
                outgoing.append(reminders.weekly_digest_email(result, [email]))

        if not outgoing:
            self.stdout.write('Nothing to send.')
            return
        failures = 0
        for msg in outgoing:
            if dry_run:
                self.stdout.write(f'--- To: {", ".join(msg.recipients)}\nSubject: {msg.subject}\n\n{msg.body}')
                continue
            try:
                reminders.send(msg)
            except Exception as exc:   # keep going so one bad address doesn't block the rest
                failures += 1
                self.stderr.write(f'Failed to send "{msg.subject}": {exc}')
            else:
                self.stdout.write(f'Sent: {msg.subject}')
        if failures:
            raise CommandError(f'{failures} email(s) failed to send.')
