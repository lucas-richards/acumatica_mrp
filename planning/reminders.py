"""Roll component plans up into one order reminder per vendor."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from .models import ComponentPlan

REMINDER_DAY_CHOICES = (7, 14, 30, 60)


class ReminderStatus(str, Enum):
    ORDER_NOW = 'Order now'
    REMINDER_ACTIVE = 'Reminder active'
    ON_TRACK = 'On track'


REMINDER_RANK = {ReminderStatus.ORDER_NOW: 0, ReminderStatus.REMINDER_ACTIVE: 1, ReminderStatus.ON_TRACK: 2}


@dataclass(frozen=True)
class VendorInfo:
    vendor_id: str
    name: str
    lead_time_days: Optional[int]


@dataclass
class VendorReminder:
    vendor: VendorInfo
    next_order_by: Optional[dt.date]     # earliest order-by of components that need ordering
    components_due: int
    due_skus: list[str]
    status: ReminderStatus
    days_until: Optional[int]            # negative = late

    @property
    def order_now(self) -> bool:
        return self.status == ReminderStatus.ORDER_NOW


def reminder_status(next_order_by: Optional[dt.date], today: dt.date, reminder_days: int) -> ReminderStatus:
    if next_order_by is None:
        return ReminderStatus.ON_TRACK
    if next_order_by <= today:
        return ReminderStatus.ORDER_NOW
    if (next_order_by - today).days <= reminder_days:
        return ReminderStatus.REMINDER_ACTIVE
    return ReminderStatus.ON_TRACK


def vendor_reminders(
    plans: Iterable[ComponentPlan],
    vendors: Iterable[VendorInfo],
    today: dt.date,
    reminder_days: int,
) -> list[VendorReminder]:
    """One reminder per vendor, most urgent first. Vendors with nothing due are included."""
    due_by_vendor: dict[str, list[ComponentPlan]] = {}
    for plan in plans:
        if plan.needs_order and plan.component.vendor_id:
            due_by_vendor.setdefault(plan.component.vendor_id, []).append(plan)

    reminders = []
    for vendor in vendors:
        due = due_by_vendor.get(vendor.vendor_id, [])
        dates = [p.order_by_date for p in due if p.order_by_date is not None]
        next_date = min(dates) if dates else None
        reminders.append(VendorReminder(
            vendor=vendor,
            next_order_by=next_date,
            components_due=len(due),
            due_skus=sorted(p.sku for p in due),
            status=reminder_status(next_date, today, reminder_days),
            days_until=(next_date - today).days if next_date else None,
        ))
    reminders.sort(key=lambda r: (
        REMINDER_RANK[r.status],
        r.next_order_by or dt.date.max,
        r.vendor.name,
    ))
    return reminders


def reminders_to_send(reminders: Iterable[VendorReminder]) -> list[VendorReminder]:
    """Vendors whose order-by date is within the reminder window (or already past)."""
    return [r for r in reminders if r.status != ReminderStatus.ON_TRACK]
