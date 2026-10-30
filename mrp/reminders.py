"""Reminder and weekly digest emails."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from planning.reminders import REMINDER_RANK, ReminderStatus, VendorReminder, reminders_to_send

from . import models, services

# Don't repeat a vendor reminder within this period unless it has become more urgent
RESEND_AFTER = dt.timedelta(days=7)


@dataclass
class Outgoing:
    subject: str
    body: str
    recipients: list[str]
    reminder: VendorReminder | None = None


def _already_sent(reminder: VendorReminder) -> bool:
    recent = models.ReminderSent.objects.filter(
        vendor_id=reminder.vendor.vendor_id, sent_at__gte=timezone.now() - RESEND_AFTER)
    current_rank = REMINDER_RANK[reminder.status]
    return any(REMINDER_RANK.get(ReminderStatus(s), 99) <= current_rank
               for s in recent.values_list('status', flat=True))


def vendor_reminder_emails(result: services.PlanResult, recipients: list[str], force: bool = False) -> list[Outgoing]:
    plans_by_sku = {p.sku: p for p in result.all_plans}
    out = []
    for r in reminders_to_send(result.reminders):
        if not force and _already_sent(r):
            continue
        when = 'now' if r.order_now else f'by {r.next_order_by:%b %-d}'
        out.append(Outgoing(
            subject=f'Order reminder: {r.vendor.name} — order {when}',
            body=render_to_string('mrp/email/vendor_reminder.txt', {
                'r': r, 'plans': [plans_by_sku[s] for s in r.due_skus], 'result': result,
                'site_url': settings.MRP_SITE_URL}),
            recipients=recipients,
            reminder=r,
        ))
    return out


def weekly_digest_email(result: services.PlanResult, recipients: list[str]) -> Outgoing:
    return Outgoing(
        subject=f'Purchasing plan for the week of {result.today:%b %-d}',
        body=render_to_string('mrp/email/weekly_digest.txt', {
            'result': result, 'to_order': [p for p in result.all_plans if p.needs_order],
            'site_url': settings.MRP_SITE_URL}),
        recipients=recipients,
    )


def send(outgoing: Outgoing) -> None:
    send_mail(outgoing.subject, outgoing.body, settings.DEFAULT_FROM_EMAIL, outgoing.recipients,
              fail_silently=False)
    if outgoing.reminder:
        r = outgoing.reminder
        models.ReminderSent.objects.create(vendor_id=r.vendor.vendor_id, status=r.status.value,
                                           order_by_date=r.next_order_by, recipients=', '.join(outgoing.recipients))
