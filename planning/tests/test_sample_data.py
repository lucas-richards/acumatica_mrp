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
               for r in data['demand'] if r['source'] == 'trailing_invoices']
    fg_demand = demand.trailing_invoices(buckets, today, 6)
    issues = demand.trailing_invoices(
        [MonthlyQty(r['sku'], to_date(r['month']), to_decimal(r['qty']))
         for r in data['demand'] if r['source'] == demand.ISSUE_SOURCE], today, 6)
    unproduced = {}
    for source, sign in (('open_so', 1), ('open_po', -1)):
        for r in data['demand']:
            if r['source'] == source:
                unproduced[r['sku']] = unproduced.get(r['sku'], D(0)) + sign * to_decimal(r['qty'])
    return build_plan_inputs(
        stock_items=data['stock_items'], on_hand=data['on_hand'], open_po_lines=data['open_po_lines'],
        bom=data['bom'], vendors=data['vendors'], item_vendors=data['item_vendors'],
        po_history=data['po_history'], fg_demand=fg_demand, issue_demand=issues,
        unproduced_orders={k: v for k, v in unproduced.items() if v > 0},
    )


def run(mode=QtyMode.COVER_TARGET):
    inputs = load_inputs()
    plans = mrp.plan_components(inputs.components, inputs.bom_lines, inputs.finished_goods, mode, TODAY)
    return inputs, plans, {p.sku: p for p in plans}


class SampleDataTests(unittest.TestCase):
    def test_fg_demand_from_trailing_invoices(self):
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
        self.assertEqual(p.committed, D(55))      # open orders for FG-100 and FG-400 with no production order
        self.assertEqual(p.supply_position, D(25))
        self.assertEqual(round(p.months_of_supply, 2), D('0.23'))
        self.assertEqual(p.status, Status.STOCKOUT_RISK)
        self.assertTrue(p.order_now)
        self.assertEqual(p.component.last_po_qty, D(600))
        self.assertEqual(round(p.target_months, 2), D('5.33'))   # 4 months lead time + a third to prepare
        self.assertEqual(round(p.needed, 1), D('561.7'))
        self.assertEqual(p.suggested_qty, D(600))

    def test_bearing_breakdown(self):
        _, _, by_sku = run()
        rows = {r.fg_sku: r for r in by_sku['COMP-2250'].breakdown}
        self.assertEqual(rows['FG-100'].qty_per, D(2))
        self.assertEqual(rows['FG-100'].fg_unproduced_orders, D(20))   # 30 on order less 10 in production
        self.assertEqual(rows['FG-100'].component_qty, D(40))
        self.assertEqual(rows['FG-400'].fg_unproduced_orders, D(15))
        self.assertEqual(rows['FG-400'].component_qty, D(15))

    def test_suggested_quantities(self):
        _, _, by_sku = run()
        # needed by the reorder point, raised to the lead time's demand plus 20% where that is more:
        # 2250: 561.7 -> 600 (floor 1.2 x 110 x 4 = 528); 2310: 158 -> 340 (336, lot 10); 4408: 174.7 -> 225
        # (211.2, lot 25); 3105: 615 -> 1,300 (1,260, lot 50); 3120: 96.7 -> 320 (312, lot 10)
        expected = {'COMP-2250': 600, 'COMP-2310': 340, 'COMP-4408': 225, 'COMP-3105': 1300, 'COMP-3120': 320}
        for sku, qty in expected.items():
            self.assertEqual(by_sku[sku].suggested_qty, D(qty), sku)
        for sku in ('COMP-5010', 'COMP-5020', 'COMP-4410', 'COMP-6010', 'COMP-3150', 'COMP-6001'):
            self.assertEqual(by_sku[sku].suggested_qty, D(0), sku)
            self.assertEqual(by_sku[sku].status, Status.COVERED, sku)

    def test_months_at_arrival_without_new_order(self):
        _, plans, by_sku = run()
        # COMP-2250: (80 available + on order - 4 months x 110 per month) / 110 = -3.27: runs out 3.27 months early.
        # The 55 on open orders with no production order are not taken off here.
        self.assertEqual(round(by_sku['COMP-2250'].months_at_arrival, 2), D('-3.27'))
        for p in plans:
            if p.status == Status.STOCKOUT_RISK:
                self.assertLess(p.months_at_arrival, 0, p.sku)
            if p.status == Status.COVERED:
                self.assertGreaterEqual(p.months_at_arrival, p.target_months - p.lead_time_months, p.sku)

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
        self.assertEqual(by_sku['COMP-2310'].order_by_date, dt.date(2026, 10, 1))
        self.assertEqual(by_sku['COMP-3105'].order_by_date, dt.date(2026, 9, 21))
        self.assertEqual(by_sku['COMP-3120'].order_by_date, dt.date(2026, 10, 23))
        self.assertEqual(by_sku['COMP-5010'].order_by_date, dt.date(2026, 11, 15))

    def test_summary_tiles(self):
        _, plans, _ = run()
        s = mrp.summarize(plans)
        self.assertEqual(s.shown, 11)
        self.assertEqual(s.components_to_order, 5)
        self.assertEqual(sorted(s.out_before_arrival), ['COMP-2250', 'COMP-2310', 'COMP-3105', 'COMP-3120', 'COMP-4408'])
        self.assertTrue(s.next_order_now)
        self.assertEqual(s.late_count, 5)
        self.assertEqual(s.vendor_count, 3)

    def test_urgency_order(self):
        _, plans, _ = run()
        self.assertEqual([p.sku for p in plans[:5]],
                         ['COMP-2250', 'COMP-4408', 'COMP-3105', 'COMP-2310', 'COMP-3120'])

    def test_vendor_cards(self):
        inputs, plans, _ = run()
        cards = {r.vendor.name: r for r in vendor_reminders(plans, inputs.vendors, TODAY, 30)}
        self.assertEqual(cards['Pacific Drives'].status, ReminderStatus.ORDER_NOW)
        self.assertEqual(cards['Pacific Drives'].components_due, 2)
        self.assertEqual(cards['Cascade Cable Co'].status, ReminderStatus.ORDER_NOW)
        self.assertEqual(cards['Cascade Cable Co'].components_due, 1)
        self.assertEqual(cards['Northwest Metals'].status, ReminderStatus.ORDER_NOW)
        self.assertEqual(cards['Northwest Metals'].next_order_by, dt.date(2026, 9, 21))
        self.assertEqual(cards['Northwest Metals'].components_due, 2)
        # Nothing is due for these two, so nothing to remind about even with a wider window
        for name in ('Inland Fasteners', 'Summit Seals'):
            self.assertEqual(cards[name].status, ReminderStatus.ON_TRACK)
            self.assertEqual(cards[name].components_due, 0)
        cards60 = {r.vendor.name: r for r in vendor_reminders(plans, inputs.vendors, TODAY, 60)}
        self.assertEqual(cards60['Summit Seals'].status, ReminderStatus.ON_TRACK)

    def test_match_last_po(self):
        _, plans, by_sku = run(mode=QtyMode.MATCH_LAST_PO)
        self.assertEqual(by_sku['COMP-2250'].suggested_qty, D(600))
        self.assertEqual(by_sku['COMP-5010'].suggested_qty, D(0))   # last-PO size only when an order is needed

    def test_purchased_finished_good_is_planned_on_invoices(self):
        data = json.loads(SAMPLE.read_text())
        for item in data['stock_items']:
            if item['sku'] == 'FG-500':
                item['replenishment_source'] = 'Purchase'
        data['item_vendors'].append({'sku': 'FG-500', 'vendor_id': data['vendors'][0]['vendor_id'], 'is_default': True,
                                     'lead_time_days': 30, 'lot_size': None})
        inputs = build_plan_inputs(
            stock_items=data['stock_items'], on_hand=data['on_hand'], open_po_lines=data['open_po_lines'],
            bom=data['bom'], vendors=data['vendors'], item_vendors=data['item_vendors'],
            po_history=data['po_history'], fg_demand={'FG-500': D(20)}, issue_demand={'FG-500': D(999)})
        by_sku = {c.sku: c for c in inputs.components}
        self.assertEqual(by_sku['FG-500'].issue_demand_per_month, D(20))   # invoices + kit breakdowns, no issues
        self.assertEqual(by_sku['FG-500'].lead_time_days, 30)
        self.assertNotIn('FG-100', by_sku)   # built finished goods are not purchased

    def test_lead_time_is_what_acumatica_has(self):
        self.assertEqual(resolve_lead_time(60, 120), 60)        # item-vendor lead time first
        self.assertEqual(resolve_lead_time(None, 120), 120)     # then the vendor's
        self.assertIsNone(resolve_lead_time(None, None))


if __name__ == '__main__':
    unittest.main()
