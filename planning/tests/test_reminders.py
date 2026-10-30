import datetime as dt
import unittest
from decimal import Decimal as D

from planning import mrp
from planning.models import BomLine, ComponentInput, FinishedGood, QtyMode
from planning.reminders import ReminderStatus, VendorInfo, reminder_status, reminders_to_send, vendor_reminders

TODAY = dt.date(2026, 10, 29)


class ReminderStatusTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(reminder_status(None, TODAY, 30), ReminderStatus.ON_TRACK)
        self.assertEqual(reminder_status(TODAY, TODAY, 30), ReminderStatus.ORDER_NOW)
        self.assertEqual(reminder_status(TODAY - dt.timedelta(days=3), TODAY, 30), ReminderStatus.ORDER_NOW)
        self.assertEqual(reminder_status(TODAY + dt.timedelta(days=30), TODAY, 30), ReminderStatus.REMINDER_ACTIVE)
        self.assertEqual(reminder_status(TODAY + dt.timedelta(days=31), TODAY, 30), ReminderStatus.ON_TRACK)
        self.assertEqual(reminder_status(TODAY + dt.timedelta(days=8), TODAY, 7), ReminderStatus.ON_TRACK)


class VendorRollupTests(unittest.TestCase):
    def test_rollup(self):
        fgs = [FinishedGood('FG', 'FG', D(0), D(10))]
        comps = [
            ComponentInput('A1', vendor_id='A', on_hand=D(5), lead_time_days=60),     # late
            ComponentInput('A2', vendor_id='A', on_hand=D(25), lead_time_days=60),    # due later
            ComponentInput('B1', vendor_id='B', on_hand=D(1000), lead_time_days=60),  # covered
        ]
        bom = [BomLine('FG', c.sku, D(1)) for c in comps]
        plans = mrp.plan_components(comps, bom, fgs, 1, QtyMode.COVER_TARGET, TODAY)
        vendors = [VendorInfo('B', 'Bravo', 60), VendorInfo('A', 'Alpha', 60), VendorInfo('C', 'Charlie', None)]
        result = vendor_reminders(plans, vendors, TODAY, 30)

        self.assertEqual([r.vendor.vendor_id for r in result], ['A', 'B', 'C'])
        a = result[0]
        self.assertEqual(a.components_due, 2)
        self.assertEqual(a.due_skus, ['A1', 'A2'])
        self.assertEqual(a.status, ReminderStatus.ORDER_NOW)
        self.assertLess(a.days_until, 0)
        self.assertEqual(result[1].components_due, 0)
        self.assertIsNone(result[1].next_order_by)
        self.assertEqual(result[1].status, ReminderStatus.ON_TRACK)
        self.assertEqual([r.vendor.vendor_id for r in reminders_to_send(result)], ['A'])


if __name__ == '__main__':
    unittest.main()
