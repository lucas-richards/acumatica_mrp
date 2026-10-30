"""The seeded sample data must reproduce the numbers on the agreed UI mockup."""
import datetime as dt
import json
import unittest
from decimal import Decimal as D
from pathlib import Path

from planning import demand, mrp
from planning.demand import MonthlyQty
from planning.inputs import build_plan_inputs, resolve_lead_time, to_date, to_decimal
from planning.models import QtyMode, Status
from planning.reminders import ReminderStatus, vendor_reminders

SAMPLE = Path(__file__).resolve().parents[2] / 'sample_data' / 'sample_data.json'
TODAY = dt.date(2026, 10, 29)


def load_inputs(today=TODAY):
    data = json.loads(SAMPLE.read_text())
    buckets = [MonthlyQty(r['sku'], to_date(r['month']), to_decimal(r['qty']))
               for r in data['demand'] if r['source'] == 'trailing_shipments']
    fg_demand = demand.trailing_shipments(buckets, today, 6)
    return build_plan_inputs(
        stock_items=data['stock_items'], on_hand=data['on_hand'], open_po_lines=data['open_po_lines'],
        bom=data['bom'], vendors=data['vendors'], item_vendors=data['item_vendors'],
        po_history=data['po_history'], fg_demand=fg_demand,
    )


def run(cover=2, mode=QtyMode.COVER_TARGET):
    inputs = load_inputs()
    plans = mrp.plan_components(inputs.components, inputs.bom_lines, inputs.finished_goods, cover, mode, TODAY)
    return inputs, plans, {p.sku: p for p in plans}


class SampleDataTests(unittest.TestCase):
    def test_fg_demand_from_trailing_shipments(self):
        inputs = load_inputs()
        demand_by_fg = {fg.sku: fg.demand_per_month for fg in inputs.finished_goods}
        self.assertEqual(demand_by_fg, {'FG-100': D(40), 'FG-200': D(25), 'FG-300': D(12),
                                        'FG-400': D(30), 'FG-500': D(20)})

    def test_bearing_row_matches_mockup(self):
        _, _, by_sku = run()
        p = by_sku['COMP-2250']
        self.assertEqual(p.component.vendor_name, 'Pacific Drives')
        self.assertEqual(len(p.breakdown), 2)
        self.assertEqual(p.usage_per_month, D(110))
        self.assertEqual(p.lead_time_months, D(4))
        self.assertEqual(p.demand_in_lead_time, D(440))
        self.assertEqual(p.component.on_hand + p.component.on_order, D(80))
        self.assertEqual(p.embedded_stock, D(120))
        self.assertEqual(round(p.months_of_supply, 1), D('1.8'))
        self.assertEqual(p.status, Status.STOCKOUT_RISK)
        self.assertTrue(p.order_now)
        self.assertEqual(p.component.last_po_qty, D(600))
        self.assertEqual(p.needed, D(460))
        self.assertEqual(p.suggested_qty, D(500))
        self.assertEqual(p.estimated_cost, D('2100.00'))

    def test_bearing_breakdown(self):
        _, _, by_sku = run()
        rows = {r.fg_sku: r for r in by_sku['COMP-2250'].breakdown}
        self.assertEqual(rows['FG-100'].qty_per, D(2))
        self.assertEqual(rows['FG-100'].component_per_month, D(80))
        self.assertEqual(rows['FG-100'].demand_in_lead_time, D(320))
        self.assertEqual(rows['FG-100'].embedded_qty, D(90))
        self.assertEqual(rows['FG-400'].component_per_month, D(30))
        self.assertEqual(rows['FG-400'].embedded_qty, D(30))

    def test_suggested_quantities(self):
        _, _, by_sku = run()
        expected = {'COMP-2250': 500, 'COMP-2310': 100, 'COMP-4408': 175, 'COMP-3105': 400,
                    'COMP-3120': 60, 'COMP-5010': 200}
        for sku, qty in expected.items():
            self.assertEqual(by_sku[sku].suggested_qty, D(qty), sku)
        for sku in ('COMP-4410', 'COMP-3150', 'COMP-5020', 'COMP-6001', 'COMP-6010'):
            self.assertEqual(by_sku[sku].suggested_qty, D(0), sku)
            self.assertEqual(by_sku[sku].status, Status.COVERED, sku)

    def test_months_at_arrival_without_new_order(self):
        _, plans, by_sku = run(cover=2)
        # COMP-2250: 200 supply / 110 per month = 1.82 months - 4 months lead time -> runs out 2.18 months early
        self.assertEqual(round(by_sku['COMP-2250'].months_at_arrival, 2), D('-2.18'))
        for p in plans:
            if p.status == Status.STOCKOUT_RISK:
                self.assertLess(p.months_at_arrival, 0, p.sku)
            if p.status == Status.COVERED:
                self.assertGreaterEqual(p.months_at_arrival, 2, p.sku)

    def test_available_quantity_replaces_on_hand(self):
        data = json.loads(SAMPLE.read_text())
        for item in data['stock_items']:
            item['qty_available'] = 10 if item['sku'] == 'COMP-2250' else None
        data['open_po_lines'][0]['order_date'] = '2026-09-01'
        inputs = build_plan_inputs(
            stock_items=data['stock_items'], on_hand=data['on_hand'], open_po_lines=data['open_po_lines'],
            bom=data['bom'], vendors=data['vendors'], item_vendors=data['item_vendors'],
            po_history=data['po_history'], fg_demand={})
        self.assertTrue(inputs.uses_available)
        by_sku = {c.sku: c for c in inputs.components}
        self.assertEqual(by_sku['COMP-2250'].on_hand, D(10))        # available, not the 50 on hand
        self.assertEqual(by_sku['COMP-2310'].on_hand, D(0))         # no available figure -> 0
        self.assertEqual(by_sku['COMP-2250'].on_order_since, dt.date(2026, 9, 1))
        self.assertFalse(load_inputs().uses_available)

    def test_order_by_dates(self):
        _, _, by_sku = run()
        self.assertEqual(by_sku['COMP-2310'].order_by_date, dt.date(2026, 11, 17))
        self.assertEqual(by_sku['COMP-3105'].order_by_date, dt.date(2026, 11, 3))
        self.assertEqual(by_sku['COMP-3120'].order_by_date, dt.date(2026, 11, 26))
        self.assertEqual(by_sku['COMP-5010'].order_by_date, dt.date(2026, 12, 14))

    def test_summary_tiles(self):
        _, plans, _ = run()
        s = mrp.summarize(plans)
        self.assertEqual(s.shown, 11)
        self.assertEqual(s.components_to_order, 6)
        self.assertEqual(sorted(s.out_before_arrival), ['COMP-2250', 'COMP-4408'])
        self.assertTrue(s.next_order_now)
        self.assertEqual(s.late_count, 2)
        self.assertEqual(s.estimated_value, D('10195.00'))
        self.assertEqual(s.vendor_count, 4)

    def test_urgency_order(self):
        _, plans, _ = run()
        self.assertEqual([p.sku for p in plans[:6]],
                         ['COMP-2250', 'COMP-4408', 'COMP-3105', 'COMP-2310', 'COMP-3120', 'COMP-5010'])

    def test_vendor_cards(self):
        inputs, plans, _ = run()
        cards = {r.vendor.name: r for r in vendor_reminders(plans, inputs.vendors, TODAY, 30)}
        self.assertEqual(cards['Pacific Drives'].status, ReminderStatus.ORDER_NOW)
        self.assertEqual(cards['Pacific Drives'].components_due, 2)
        self.assertEqual(cards['Cascade Cable Co'].status, ReminderStatus.ORDER_NOW)
        self.assertEqual(cards['Cascade Cable Co'].components_due, 1)
        self.assertEqual(cards['Northwest Metals'].status, ReminderStatus.REMINDER_ACTIVE)
        self.assertEqual(cards['Northwest Metals'].next_order_by, dt.date(2026, 11, 3))
        self.assertEqual(cards['Northwest Metals'].components_due, 2)
        self.assertEqual(cards['Inland Fasteners'].status, ReminderStatus.ON_TRACK)
        self.assertEqual(cards['Inland Fasteners'].next_order_by, dt.date(2026, 12, 14))
        self.assertEqual(cards['Summit Seals'].status, ReminderStatus.ON_TRACK)
        self.assertEqual(cards['Summit Seals'].components_due, 0)
        # A 60-day window turns the Inland Fasteners reminder on
        cards60 = {r.vendor.name: r for r in vendor_reminders(plans, inputs.vendors, TODAY, 60)}
        self.assertEqual(cards60['Inland Fasteners'].status, ReminderStatus.REMINDER_ACTIVE)

    def test_cover_lead_time_only(self):
        _, plans, by_sku = run(cover=0)
        # Only components that run out before an order would arrive need ordering
        self.assertEqual(sorted(p.sku for p in plans if p.needs_order), ['COMP-2250', 'COMP-4408'])
        self.assertEqual(by_sku['COMP-2250'].suggested_qty, D(250))   # 440 - 200 = 240 -> lot 50

    def test_match_last_po(self):
        _, plans, by_sku = run(mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(by_sku['COMP-2250'].suggested_qty, D(600))
        self.assertEqual(by_sku['COMP-5010'].suggested_qty, D(500))
        self.assertEqual(by_sku['COMP-4410'].suggested_qty, D(0))

    def test_lead_time_override(self):
        inputs = load_inputs()
        self.assertEqual(set(inputs.lead_time_sources.values()), {'vendor'})
        self.assertEqual(resolve_lead_time(45, 60, 120), (45, 'override'))
        self.assertEqual(resolve_lead_time(None, 60, 120), (60, 'item_vendor'))
        self.assertEqual(resolve_lead_time(None, None, None), (None, 'missing'))


if __name__ == '__main__':
    unittest.main()
