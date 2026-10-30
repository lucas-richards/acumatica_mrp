import datetime as dt
import unittest
from decimal import Decimal as D

from planning import mrp
from planning.models import BomLine, ComponentInput, FinishedGood, Flag, QtyMode, Status

TODAY = dt.date(2026, 10, 29)


class FormulaTests(unittest.TestCase):
    def test_lead_time_months(self):
        self.assertEqual(mrp.lead_time_months(120), D(4))
        self.assertEqual(mrp.lead_time_months(15), D('0.5'))
        self.assertEqual(mrp.lead_time_months(0), D(0))
        self.assertEqual(mrp.lead_time_months(None), D(0))
        self.assertEqual(mrp.lead_time_months(-5), D(0))

    def test_demand_in_lead_time(self):
        self.assertEqual(mrp.demand_in_lead_time(D(110), D(4)), D(440))

    def test_supply_position(self):
        self.assertEqual(mrp.supply_position(D(50), D(30), D(120)), D(200))

    def test_months_of_supply(self):
        self.assertEqual(mrp.months_of_supply(D(220), D(110)), D(2))
        self.assertIsNone(mrp.months_of_supply(D(100), D(0)))
        self.assertIsNone(mrp.months_of_supply(D(100), D(-1)))

    def test_target_months(self):
        self.assertEqual(mrp.target_months(D(4), 2), D(6))
        self.assertEqual(mrp.target_months(D(4), 0), D(4))
        with self.assertRaises(ValueError):
            mrp.target_months(D(4), 5)
        with self.assertRaises(ValueError):
            mrp.target_months(D(4), -1)

    def test_needed_qty(self):
        self.assertEqual(mrp.needed_qty(D(110), D(6), D(200)), D(460))
        self.assertEqual(mrp.needed_qty(D(10), D(2), D(50)), D(-30))

    def test_round_up_to_lot(self):
        self.assertEqual(mrp.round_up_to_lot(D(460), D(50)), D(500))
        self.assertEqual(mrp.round_up_to_lot(D(500), D(50)), D(500))
        self.assertEqual(mrp.round_up_to_lot(D('0.1'), D(50)), D(50))
        self.assertEqual(mrp.round_up_to_lot(D(0), D(50)), D(0))
        self.assertEqual(mrp.round_up_to_lot(D(-10), D(50)), D(0))

    def test_round_up_to_lot_missing_or_bad_lot_size_uses_one(self):
        self.assertEqual(mrp.round_up_to_lot(D('7.2'), None), D(8))
        self.assertEqual(mrp.round_up_to_lot(D('7.2'), D(0)), D(8))
        self.assertEqual(mrp.round_up_to_lot(D('7.2'), D(-5)), D(8))

    def test_round_up_to_fractional_lot(self):
        self.assertEqual(mrp.round_up_to_lot(D('1.1'), D('0.5')), D('1.5'))

    def test_order_by_date_future(self):
        # supply 1085 / usage 210 = 5.1667 months; lead time 150 days -> 5 days left
        self.assertEqual(mrp.order_by_date(TODAY, D(1085), D(210), 150), dt.date(2026, 11, 3))

    def test_order_by_date_floors_fractional_days(self):
        # 325/70 x 30 = 139.29 days of supply - 120 lead = 19.29 -> 19 days
        self.assertEqual(mrp.order_by_date(TODAY, D(325), D(70), 120), dt.date(2026, 11, 17))

    def test_order_by_date_past(self):
        self.assertLess(mrp.order_by_date(TODAY, D(200), D(110), 120), TODAY)

    def test_order_by_date_no_usage(self):
        self.assertIsNone(mrp.order_by_date(TODAY, D(100), D(0), 120))

    def test_order_by_date_missing_lead_time_counts_as_zero(self):
        self.assertEqual(mrp.order_by_date(TODAY, D(30), D(30), None), TODAY + dt.timedelta(days=30))

    def test_status(self):
        self.assertEqual(mrp.plan_status(D('1.8'), D(4), D(6)), Status.STOCKOUT_RISK)
        self.assertEqual(mrp.plan_status(D('4.0'), D(4), D(6)), Status.ORDER_SOON)
        self.assertEqual(mrp.plan_status(D('5.99'), D(4), D(6)), Status.ORDER_SOON)
        self.assertEqual(mrp.plan_status(D('6.0'), D(4), D(6)), Status.COVERED)
        self.assertEqual(mrp.plan_status(None, D(4), D(6)), Status.COVERED)


def component(**kwargs):
    defaults = dict(sku='C1', vendor_id='V1', vendor_name='Vendor 1', on_hand=D(0), on_order=D(0),
                    lead_time_days=30, lot_size=D(1), unit_cost=D('2.00'))
    defaults.update(kwargs)
    return ComponentInput(**defaults)


class PlanComponentTests(unittest.TestCase):
    def plan(self, comp, bom=None, fgs=None, cover=1, mode=QtyMode.COVER_TARGET):
        bom = bom if bom is not None else [BomLine('FG1', comp.sku, D(2))]
        fgs = fgs if fgs is not None else [FinishedGood('FG1', 'Finished 1', D(5), D(10))]
        return mrp.plan_component(comp, bom, {f.sku: f for f in fgs}, cover, mode, TODAY)

    def test_basic_plan(self):
        p = self.plan(component(on_hand=D(10), on_order=D(5)))
        self.assertEqual(p.usage_per_month, D(20))
        self.assertEqual(p.embedded_stock, D(10))
        self.assertEqual(p.supply_position, D(25))
        self.assertEqual(p.months_of_supply, D('1.25'))
        self.assertEqual(p.target_months, D(2))
        self.assertEqual(p.needed, D(15))
        self.assertEqual(p.suggested_qty, D(15))
        self.assertEqual(p.estimated_cost, D('30.00'))
        self.assertEqual(p.status, Status.ORDER_SOON)
        self.assertEqual(p.flags, set())

    def test_breakdown_rows(self):
        bom = [BomLine('FG1', 'C1', D(2)), BomLine('FG2', 'C1', D(1))]
        fgs = [FinishedGood('FG1', 'One', D(5), D(10)), FinishedGood('FG2', 'Two', D(3), D(40))]
        p = self.plan(component(lead_time_days=60), bom=bom, fgs=fgs)
        self.assertEqual([r.fg_sku for r in p.breakdown], ['FG2', 'FG1'])  # largest usage first
        fg2 = p.breakdown[0]
        self.assertEqual(fg2.component_per_month, D(40))
        self.assertEqual(fg2.demand_in_lead_time, D(80))
        self.assertEqual(fg2.embedded_qty, D(3))
        self.assertEqual(p.usage_per_month, D(60))
        self.assertEqual(p.embedded_stock, D(13))

    def test_covered_component_suggests_zero(self):
        p = self.plan(component(on_hand=D(1000)))
        self.assertEqual(p.suggested_qty, D(0))
        self.assertEqual(p.estimated_cost, D(0))
        self.assertEqual(p.status, Status.COVERED)
        self.assertFalse(p.needs_order)

    def test_months_at_arrival(self):
        p = self.plan(component(on_hand=D(10), on_order=D(5)))
        # supply 25 + order 15 = 40, usage 20/mo -> 2 months, minus 1 month lead time
        self.assertEqual(p.months_at_arrival, D(1))
        covered = self.plan(component(on_hand=D(1000)))
        self.assertEqual(covered.months_at_arrival, D('49.5'))   # 1010 / 20 - 1

    def test_zero_usage_does_not_crash_and_is_flagged(self):
        p = self.plan(component(on_hand=D(5)), fgs=[FinishedGood('FG1', 'One', D(0), D(0))])
        self.assertIsNone(p.months_at_arrival)
        self.assertIsNone(p.months_of_supply)
        self.assertIsNone(p.order_by_date)
        self.assertFalse(p.order_now)
        self.assertEqual(p.suggested_qty, D(0))
        self.assertEqual(p.status, Status.COVERED)
        self.assertIn(Flag.NO_USAGE, p.flags)

    def test_component_not_in_any_bom(self):
        p = self.plan(component(), bom=[])
        self.assertIn(Flag.NO_BOM, p.flags)
        self.assertNotIn(Flag.NO_USAGE, p.flags)
        self.assertEqual(p.usage_per_month, D(0))

    def test_fg_missing_from_master_or_demand(self):
        p = self.plan(component(), fgs=[])
        self.assertIn(Flag.MISSING_FG_DEMAND, p.flags)
        self.assertEqual(p.usage_per_month, D(0))
        p = self.plan(component(), fgs=[FinishedGood('FG1', 'One', D(2), None)])
        self.assertIn(Flag.MISSING_FG_DEMAND, p.flags)
        self.assertEqual(p.embedded_stock, D(4))

    def test_missing_lead_time_treated_as_zero(self):
        p = self.plan(component(lead_time_days=None))
        self.assertIn(Flag.MISSING_LEAD_TIME, p.flags)
        self.assertEqual(p.lead_time_months, D(0))
        self.assertEqual(p.target_months, D(1))

    def test_missing_vendor_and_cost(self):
        p = self.plan(component(vendor_id=None, vendor_name=None, unit_cost=None))
        self.assertIn(Flag.NO_VENDOR, p.flags)
        self.assertIn(Flag.MISSING_COST, p.flags)
        self.assertIsNone(p.estimated_cost)

    def test_missing_cost_not_flagged_when_nothing_to_order(self):
        p = self.plan(component(unit_cost=None, on_hand=D(1000)))
        self.assertNotIn(Flag.MISSING_COST, p.flags)

    def test_match_last_po_uses_previous_quantity(self):
        p = self.plan(component(last_po_qty=D(250)), mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(p.suggested_qty, D(250))
        self.assertEqual(p.estimated_cost, D('500.00'))

    def test_match_last_po_only_when_order_needed(self):
        p = self.plan(component(on_hand=D(1000), last_po_qty=D(250)), mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(p.suggested_qty, D(0))

    def test_match_last_po_without_history_falls_back(self):
        p = self.plan(component(lot_size=D(10)), mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(p.suggested_qty, D(30))   # 20 x 2 - 10 embedded = 30, lot 10
        self.assertIn(Flag.NO_PO_HISTORY, p.flags)

    def test_cover_months_changes_target(self):
        low = self.plan(component(on_hand=D(30)), cover=0)
        high = self.plan(component(on_hand=D(30)), cover=4)
        self.assertEqual(low.suggested_qty, D(0))
        # usage 20/mo, target 1 + 4 = 5 months, supply 30 on hand + 10 embedded = 40
        self.assertEqual(high.needed, D(60))
        self.assertEqual(high.suggested_qty, D(60))

    def test_order_now(self):
        p = self.plan(component(lead_time_days=120))
        self.assertTrue(p.order_now)
        self.assertEqual(p.status, Status.STOCKOUT_RISK)


class SortAndSummaryTests(unittest.TestCase):
    def test_urgency_sort_and_summary(self):
        fgs = [FinishedGood('FG1', 'One', D(0), D(10))]
        comps = [
            component(sku='COVERED', on_hand=D(1000)),
            component(sku='SOON', on_hand=D(15), lead_time_days=30),
            component(sku='STOCKOUT', on_hand=D(5), lead_time_days=60, unit_cost=None),
            component(sku='NOUSE'),
        ]
        bom = [BomLine('FG1', c.sku, D(1)) for c in comps if c.sku != 'NOUSE']
        plans = mrp.plan_components(comps, bom, fgs, 1, QtyMode.COVER_TARGET, TODAY)
        self.assertEqual([p.sku for p in plans], ['STOCKOUT', 'SOON', 'COVERED', 'NOUSE'])
        s = mrp.summarize(plans)
        self.assertEqual(s.shown, 4)
        self.assertEqual(s.components_to_order, 2)
        self.assertEqual(s.out_before_arrival, ['STOCKOUT'])
        self.assertTrue(s.next_order_now)
        self.assertEqual(s.late_count, 1)
        self.assertEqual(s.missing_cost_count, 1)
        self.assertEqual(s.vendor_count, 1)

    def test_summary_empty(self):
        s = mrp.summarize([])
        self.assertEqual(s.components_to_order, 0)
        self.assertIsNone(s.next_order_by)
        self.assertEqual(s.estimated_value, D(0))


if __name__ == '__main__':
    unittest.main()
