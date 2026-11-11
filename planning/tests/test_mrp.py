import dataclasses
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
        # never less than 7 days, even with no lead time
        self.assertEqual(mrp.lead_time_months(7), D(7) / 30)
        self.assertEqual(mrp.lead_time_months(0), D(7) / 30)
        self.assertEqual(mrp.lead_time_months(None), D(7) / 30)
        self.assertEqual(mrp.lead_time_months(-5), D(7) / 30)

    def test_demand_in_lead_time(self):
        self.assertEqual(mrp.demand_in_lead_time(D(110), D(4)), D(440))

    def test_supply_position(self):
        self.assertEqual(mrp.supply_position(D(50), D(30), D(120)), D(-40))

    def test_months_of_supply(self):
        self.assertEqual(mrp.months_of_supply(D(220), D(110)), D(2))
        self.assertIsNone(mrp.months_of_supply(D(100), D(0)))
        self.assertIsNone(mrp.months_of_supply(D(100), D(-1)))

    def test_target_months(self):
        # a week or less of lead time -> 14 days
        for days in (None, 0, 3, 7):
            self.assertEqual(mrp.target_months(days), D(14) / 30, days)
        self.assertEqual(mrp.target_months(21), D(28) / 30)      # a third of the lead time, at least 7 days
        self.assertEqual(mrp.target_months(90), D(4))            # 3 months -> 4 months
        self.assertEqual(mrp.target_months(180), D(8))           # the longer it takes, the more to prepare

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
                    lead_time_days=90, lot_size=D(1), issue_demand_per_month=D(20))
    defaults.update(kwargs)
    return ComponentInput(**defaults)


class PlanComponentTests(unittest.TestCase):
    def plan(self, comp, bom=None, fgs=None, mode=QtyMode.COVER_TARGET):
        bom = bom if bom is not None else [BomLine('FG1', comp.sku, D(2))]
        fgs = fgs if fgs is not None else [FinishedGood('FG1', 'Finished 1')]
        return mrp.plan_component(comp, bom, {f.sku: f for f in fgs}, mode, TODAY)

    def test_basic_plan(self):
        p = self.plan(component(on_hand=D(60), on_order=D(5)))
        self.assertEqual(p.usage_per_month, D(20))
        self.assertEqual(p.committed, D(0))
        self.assertEqual(p.supply_position, D(65))
        self.assertEqual(p.months_of_supply, D('3.25'))
        self.assertEqual(p.lead_time_months, D(3))
        self.assertEqual(p.target_months, D(4))
        self.assertEqual(p.needed, D(15))
        self.assertEqual(p.suggested_qty, D(72))      # but never less than 1.2 x 20/mo x 3 months
        self.assertEqual(p.status, Status.ORDER_SOON)
        self.assertEqual(p.flags, set())

    def test_usage_is_issue_history_only(self):
        # finished-good demand no longer drives usage
        p = self.plan(component(issue_demand_per_month=D(7)), fgs=[FinishedGood('FG1', 'One', demand_per_month=D(500))])
        self.assertEqual(p.usage_per_month, D(7))
        self.assertEqual(self.plan(component(issue_demand_per_month=None)).usage_per_month, D(0))

    def test_open_orders_without_production_are_committed(self):
        bom = [BomLine('FG1', 'C1', D(2)), BomLine('FG2', 'C1', D(1))]
        fgs = [FinishedGood('FG1', 'One', unproduced_orders=D(5)), FinishedGood('FG2', 'Two', unproduced_orders=D(3))]
        p = self.plan(component(on_hand=D(100)), bom=bom, fgs=fgs)
        self.assertEqual([r.fg_sku for r in p.breakdown], ['FG1', 'FG2'])   # largest need first
        self.assertEqual(p.breakdown[0].component_qty, D(10))
        self.assertEqual(p.committed, D(13))
        self.assertEqual(p.supply_position, D(87))
        self.assertEqual(p.usage_per_month, D(20))   # unchanged by the open orders

    def test_breakdown_shows_fg_stock_and_last_run(self):
        fgs = [FinishedGood('FG1', 'One', on_hand=D(30), demand_per_month=D(40), last_run_qty=D(100),
                            last_run_date=dt.date(2026, 9, 1)),
               FinishedGood('FG2', 'Two', on_hand=D(200), demand_per_month=D(40))]
        bom = [BomLine('FG1', 'C1', D(2)), BomLine('FG2', 'C1', D(1))]
        p = self.plan(component(), bom=bom, fgs=fgs)
        rows = {r.fg_sku: r for r in p.breakdown}
        self.assertEqual(rows['FG1'].months_of_stock, D('0.75'))
        self.assertTrue(rows['FG1'].is_low)
        self.assertEqual(rows['FG1'].last_run_component_qty, D(200))   # 100 units x 2
        self.assertEqual(rows['FG2'].months_of_stock, D(5))
        self.assertFalse(rows['FG2'].is_low)
        self.assertIsNone(rows['FG2'].last_run_component_qty)

    def test_bought_finished_good_has_no_impact_rows(self):
        bom = [BomLine('FG1', 'FG1', D(1))]
        p = self.plan(component(sku='FG1', is_finished_good=True, issue_demand_per_month=D(30)), bom=bom)
        self.assertEqual(p.breakdown, [])
        self.assertEqual(p.usage_per_month, D(30))
        self.assertNotIn(Flag.NO_BOM, p.flags)

    def test_covered_component_suggests_zero(self):
        p = self.plan(component(on_hand=D(1000)))
        self.assertEqual(p.suggested_qty, D(0))
        self.assertEqual(p.status, Status.COVERED)
        self.assertFalse(p.needs_order)

    def test_months_at_arrival(self):
        p = self.plan(component(on_hand=D(60), on_order=D(5)))
        # supply 65, usage 20/mo -> 3.25 months, minus 3 months lead time
        self.assertEqual(p.months_at_arrival, D('0.25'))
        short = self.plan(component(on_hand=D(10), lead_time_days=180))
        self.assertEqual(short.months_at_arrival, D('-5.5'))   # 10 / 20 = 0.5 - 6 months
        covered = self.plan(component(on_hand=D(1010)))
        self.assertEqual(covered.months_at_arrival, D('47.5'))   # 1010 / 20 - 3

    def test_months_left_with_the_order(self):
        p = self.plan(component(on_hand=D(60), on_order=D(5)))   # supply 65, usage 20, lead 3 months
        self.assertEqual(p.months_at_arrival, D('0.25'))
        self.assertEqual(p.months_at_arrival_with(D(0)), D('0.25'))
        self.assertEqual(p.months_at_arrival_with(D(40)), D('2.25'))   # (65 + 40) / 20 - 3
        self.assertEqual(p.months_at_arrival_with_default, p.months_at_arrival_with(p.default_order_qty))
        none = self.plan(component(issue_demand_per_month=D(0)))
        self.assertIsNone(none.months_at_arrival_with(D(100)))

    def test_stock_in_receiving_counts_as_supply(self):
        p = self.plan(component(on_hand=D(10), on_order=D(5), in_receiving=D(45)))
        self.assertEqual(p.supply_position, D(60))      # 10 + 5 + 45
        self.assertEqual(p.months_of_supply, D(3))
        self.assertEqual(mrp.supply_position(D(10), D(5), D(0), D(45)), D(60))

    def test_months_left_needs_a_lead_time_and_a_demand(self):
        ok = self.plan(component(on_hand=D(100)))
        self.assertEqual(ok.missing_for_months_left, '')
        self.assertIsNotNone(ok.months_at_arrival)
        no_lead = self.plan(component(on_hand=D(100), lead_time_days=None))
        self.assertEqual(no_lead.missing_for_months_left, 'lead time')
        self.assertIsNone(no_lead.months_at_arrival)
        self.assertIsNone(no_lead.months_at_arrival_with(D(50)))
        no_demand = self.plan(component(on_hand=D(100), issue_demand_per_month=D(0)))
        self.assertEqual(no_demand.missing_for_months_left, 'demand')
        self.assertIsNone(no_demand.months_at_arrival_with(D(50)))
        both = self.plan(component(on_hand=D(100), lead_time_days=None, issue_demand_per_month=D(0)))
        self.assertEqual(both.missing_for_months_left, 'lead time and demand')
        # the rest of the plan still works with the 7-day minimum
        self.assertEqual(no_lead.lead_time_months, D(7) / 30)

    def test_months_left_formula(self):
        # (available + on order + in Receiving - lead time x usage) / usage; lead time 3 months, usage 20
        p = self.plan(component(on_hand=D(100), on_order=D(40), in_receiving=D(20)))
        self.assertEqual(p.stock_for_arrival, D(160))
        self.assertEqual(p.months_at_arrival, D(5))                   # (160 - 60) / 20
        self.assertEqual(p.months_at_arrival_with(D(100)), D(10))     # (160 + 100 - 60) / 20
        self.assertEqual(p.months_at_arrival_with(D(0)), p.months_at_arrival)

    def test_open_orders_do_not_change_months_left(self):
        bom = [BomLine('FG1', 'C1', D(2))]
        none = self.plan(component(on_hand=D(100)), bom=bom, fgs=[FinishedGood('FG1', 'One')])
        orders = self.plan(component(on_hand=D(100)), bom=bom, fgs=[FinishedGood('FG1', 'One', unproduced_orders=D(30))])
        self.assertEqual(orders.committed, D(60))
        self.assertLess(orders.supply_position, none.supply_position)       # months of supply still counts them
        self.assertEqual(orders.months_at_arrival, none.months_at_arrival)

    def test_less_usage_never_means_fewer_months_left_when_stock_is_not_negative(self):
        for on_hand in (D(0), D(15), D(250), D(5000)):
            for lead in (14, 90, 180):
                high = self.plan(component(on_hand=on_hand, on_order=D(10), lead_time_days=lead,
                                           issue_demand_per_month=D(40)))
                low = self.plan(component(on_hand=on_hand, on_order=D(10), lead_time_days=lead,
                                          issue_demand_per_month=D(10)))
                self.assertGreater(low.months_at_arrival, high.months_at_arrival)
                self.assertGreater(low.months_at_arrival_with(D(100)), high.months_at_arrival_with(D(100)))

    def test_zero_usage_does_not_crash_and_is_flagged(self):
        p = self.plan(component(on_hand=D(5), issue_demand_per_month=D(0)))
        self.assertIsNone(p.months_at_arrival)
        self.assertIsNone(p.months_of_supply)
        self.assertIsNone(p.order_by_date)
        self.assertFalse(p.order_now)
        self.assertEqual(p.suggested_qty, D(0))
        self.assertEqual(p.status, Status.COVERED)
        self.assertIn(Flag.NO_USAGE, p.flags)

    def test_negative_available_is_a_shortage_even_without_usage(self):
        p = self.plan(component(on_hand=D(-50), lot_size=D(10), issue_demand_per_month=D(0)))
        self.assertEqual(p.status, Status.STOCKOUT_RISK)
        self.assertEqual(p.suggested_qty, D(50))
        self.assertIsNone(p.months_at_arrival)

    def test_component_not_in_any_bom_and_never_issued(self):
        p = self.plan(component(issue_demand_per_month=None), bom=[])
        self.assertIn(Flag.NO_BOM, p.flags)
        self.assertNotIn(Flag.NO_USAGE, p.flags)
        self.assertEqual(p.usage_per_month, D(0))

    def test_issued_but_in_no_bom(self):
        p = self.plan(component(issue_demand_per_month=D(7)), bom=[])
        self.assertEqual(p.usage_per_month, D(7))
        self.assertNotIn(Flag.NO_BOM, p.flags)

    def test_missing_lead_time_uses_the_one_week_minimum(self):
        p = self.plan(component(lead_time_days=None))
        self.assertIn(Flag.MISSING_LEAD_TIME, p.flags)
        self.assertEqual(p.lead_time_months, D(7) / 30)
        self.assertEqual(p.target_months, D(14) / 30)

    def test_missing_vendor(self):
        p = self.plan(component(vendor_id=None, vendor_name=None))
        self.assertIn(Flag.NO_VENDOR, p.flags)

    def test_order_by_date_out_of_calendar_range(self):
        self.assertIsNone(mrp.order_by_date(TODAY, D('1e12'), D('0.0001'), 10))
        self.assertEqual(mrp.order_by_date(TODAY, D('-1e12'), D('0.0001'), 10), dt.date.min)

    def test_po_late_when_promised_date_has_passed(self):
        late = self.plan(component(on_order=D(10), earliest_promised=TODAY - dt.timedelta(days=1)))
        self.assertIn(Flag.PO_LATE, late.flags)
        for promised in (TODAY, TODAY + dt.timedelta(days=5), None):
            self.assertNotIn(Flag.PO_LATE, self.plan(component(on_order=D(10), earliest_promised=promised)).flags)

    def test_match_last_po_uses_previous_quantity(self):
        p = self.plan(component(last_po_qty=D(250)), mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(p.suggested_qty, D(250))

    def test_match_last_po_only_when_order_needed(self):
        p = self.plan(component(on_hand=D(1000), last_po_qty=D(250)), mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(p.suggested_qty, D(0))

    def test_match_last_po_without_history_falls_back(self):
        p = self.plan(component(on_hand=D(10), lot_size=D(10)), mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(p.suggested_qty, D(80))   # 20 x 4 - 10 = 70 is under 1.2 x 20 x 3 = 72, so 72 in lots of 10
        self.assertIn(Flag.NO_PO_HISTORY, p.flags)

    def test_chosen_usage_drives_the_plan(self):
        comp = component(on_hand=D(60), on_order=D(5), long_term_usage=D(10), custom_usage=D(40))
        six = self.plan(comp)
        self.assertEqual((six.usage_basis, six.usage_per_month), ('six', D(20)))
        two = self.plan(dataclasses.replace(comp, usage_basis='two_year'))
        self.assertEqual((two.usage_basis, two.usage_per_month), ('two_year', D(10)))
        self.assertEqual(two.months_of_supply, D('6.5'))          # 65 / 10
        custom = self.plan(dataclasses.replace(comp, usage_basis='custom'))
        self.assertEqual((custom.usage_basis, custom.usage_per_month), ('custom', D(40)))
        self.assertEqual(custom.needed, D(95))                    # 40 x 4 months - 65
        # a choice with nothing behind it falls back to the recent usage
        for basis, extra in (('two_year', {'long_term_usage': None}), ('custom', {'custom_usage': None})):
            fallback = self.plan(dataclasses.replace(comp, usage_basis=basis, **extra))
            self.assertEqual((fallback.usage_basis, fallback.usage_per_month), ('six', D(20)))
        # a custom usage of zero is a real choice: nothing is used
        zero = self.plan(dataclasses.replace(comp, usage_basis='custom', custom_usage=D(0)))
        self.assertEqual(zero.usage_per_month, D(0))
        self.assertIn(Flag.NO_USAGE, zero.flags)

    def test_prepare_days_is_the_gap_between_the_two_lines(self):
        self.assertEqual(self.plan(component(lead_time_days=90)).prepare_days, 30)    # LT 90d, reorder 120d
        self.assertEqual(self.plan(component(lead_time_days=7)).prepare_days, 7)
        self.assertEqual(self.plan(component(lead_time_days=None)).prepare_days, 7)
        self.assertEqual(self.plan(component(lead_time_days=180)).prepare_days, 60)

    def test_an_order_is_at_least_the_lead_time_demand_plus_20_percent(self):
        # usage 20/mo, lead time 3 months: the least worth ordering is 1.2 x 20 x 3 = 72
        self.assertEqual(mrp.minimum_order(D(20), D(3)), D(72))
        # plenty of stock but under the reorder point: the small shortfall is raised to the minimum
        near = self.plan(component(on_hand=D(75)))
        self.assertEqual(near.needed, D(5))
        self.assertEqual(near.suggested_qty, D(72))
        # a big shortfall is left as it is
        far = self.plan(component(on_hand=D(0)))
        self.assertEqual(far.suggested_qty, D(80))
        # nothing is ordered when stock covers the reorder point
        covered = self.plan(component(on_hand=D(500)))
        self.assertEqual(covered.suggested_qty, D(0))
        # the minimum respects the lot size
        lots = self.plan(component(on_hand=D(75), lot_size=D(50)))
        self.assertEqual(lots.suggested_qty, D(100))
        # no usage, no minimum: an already negative stock is just brought back to zero
        idle = self.plan(component(on_hand=D(-50), issue_demand_per_month=D(0)))
        self.assertEqual(idle.suggested_qty, D(50))

    def test_longer_lead_time_means_more_time_to_prepare(self):
        short = self.plan(component(on_hand=D(30), lead_time_days=7))
        long = self.plan(component(on_hand=D(30), lead_time_days=90))
        # usage 20/mo: 14 days of cover needs 9.33, so nothing; 4 months needs 80 - 30 = 50, raised to the
        # lead time's demand plus 20% (1.2 x 20 x 3 = 72)
        self.assertEqual(short.suggested_qty, D(0))
        self.assertEqual(long.needed, D(50))
        self.assertEqual(long.suggested_qty, D(72))

    def test_the_order_box_always_starts_at_zero(self):
        # not the last order, not the suggestion: the planner types what to order
        self.assertEqual(self.plan(component(last_po_qty=D(250))).default_order_qty, D(0))
        needs = self.plan(component(last_po_qty=None))
        self.assertGreater(needs.suggested_qty, 0)
        self.assertEqual(needs.default_order_qty, D(0))
        self.assertEqual(self.plan(component(on_hand=D(1000), last_po_qty=D(250))).default_order_qty, D(0))

    def test_vendor_warnings(self):
        base = dict(has_vendor_rows=True, default_vendor_id='V1', default_vendor_name='Vendor 1', last_po_vendor_id='V1')
        self.assertNotIn(Flag.VENDOR_MISMATCH, self.plan(component(**base)).flags)            # same vendor
        self.assertNotIn(Flag.VENDOR_MISMATCH, self.plan(component(**{**base, 'last_po_vendor_id': ' v1 '})).flags)
        self.assertNotIn(Flag.VENDOR_MISMATCH, self.plan(component(**{**base, 'last_po_vendor_id': None})).flags)   # no PO yet
        other = self.plan(component(**{**base, 'last_po_vendor_id': 'V2'}))
        self.assertIn(Flag.VENDOR_MISMATCH, other.flags)
        self.assertNotIn(Flag.NO_DEFAULT_VENDOR, other.flags)
        # vendors listed but none marked default
        nodefault = self.plan(component(has_vendor_rows=True, default_vendor_id=None, last_po_vendor_id='V2'))
        self.assertIn(Flag.NO_DEFAULT_VENDOR, nodefault.flags)
        self.assertNotIn(Flag.VENDOR_MISMATCH, nodefault.flags)
        # no vendor at all is the existing 'No vendor' flag, not this one
        novendor = self.plan(component(vendor_id=None, vendor_name=None, has_vendor_rows=False))
        self.assertNotIn(Flag.NO_DEFAULT_VENDOR, novendor.flags)
        # marked done: hidden
        done = self.plan(component(**{**base, 'last_po_vendor_id': 'V2', 'acked_warnings': frozenset({'vendor_mismatch'})}))
        self.assertNotIn(Flag.VENDOR_MISMATCH, done.flags)
        done = self.plan(component(has_vendor_rows=True, acked_warnings=frozenset({'no_default_vendor'})))
        self.assertNotIn(Flag.NO_DEFAULT_VENDOR, done.flags)

    def test_order_now(self):
        p = self.plan(component(lead_time_days=120))
        self.assertTrue(p.order_now)
        self.assertEqual(p.status, Status.STOCKOUT_RISK)


class SortAndSummaryTests(unittest.TestCase):
    def test_urgency_sort_and_summary(self):
        fgs = [FinishedGood('FG1', 'One')]
        comps = [
            component(sku='COVERED', on_hand=D(1000), issue_demand_per_month=D(10)),
            component(sku='SOON', on_hand=D(12), lead_time_days=30, issue_demand_per_month=D(10)),
            component(sku='STOCKOUT', on_hand=D(5), lead_time_days=60, issue_demand_per_month=D(10)),
            component(sku='NOUSE', issue_demand_per_month=D(0)),
        ]
        bom = [BomLine('FG1', c.sku, D(1)) for c in comps if c.sku != 'NOUSE']
        plans = mrp.plan_components(comps, bom, fgs, QtyMode.COVER_TARGET, TODAY)
        self.assertEqual([p.sku for p in plans], ['STOCKOUT', 'SOON', 'COVERED', 'NOUSE'])
        s = mrp.summarize(plans)
        self.assertEqual(s.shown, 4)
        self.assertEqual(s.components_to_order, 2)
        self.assertEqual(s.out_before_arrival, ['STOCKOUT'])
        self.assertTrue(s.next_order_now)
        self.assertEqual(s.late_count, 1)
        self.assertEqual(s.vendor_count, 1)

    def test_summary_empty(self):
        s = mrp.summarize([])
        self.assertEqual(s.components_to_order, 0)
        self.assertIsNone(s.next_order_by)


if __name__ == '__main__':
    unittest.main()
