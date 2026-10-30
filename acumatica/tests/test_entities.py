import datetime as dt
import unittest
from decimal import Decimal as D

from acumatica import entities
from acumatica.entities import FetchOptions, Units, explode, fetch_all
from acumatica.rest import AcumaticaError, AcumaticaPermissionError

TODAY = dt.date(2026, 10, 30)


class FakeClient:
    """Returns already-flattened records keyed by entity name and records each call."""

    def __init__(self, records, deny=()):
        self.records = records
        self.deny = set(deny)
        self.calls = []

    def fetch(self, endpoint, entity, *, select=None, expand=None, filter=''):
        self.calls.append((endpoint, entity, filter))
        if entity in self.deny:
            raise AcumaticaPermissionError(f'{entity}: You have insufficient rights', 403)
        return self.records.get(entity, [])


def item(sku, base='EACH', conversions=(), warehouses=(), vendors=(), last_cost=None, **extra):
    return {'InventoryID': sku, 'Description': f'{sku} desc', 'ItemClass': 'CLS', 'BaseUOM': base,
            'LastCost': last_cost, 'UOMConversions': list(conversions), 'WarehouseDetails': list(warehouses),
            'VendorDetails': list(vendors), **extra}


def conv(from_uom, factor, op='Multiply', to='EACH'):
    return {'FromUOM': from_uom, 'ToUOM': to, 'MultiplyOrDivide': op, 'ConversionFactor': factor}


def material(sku, qty, uom='EACH', batch=1, **extra):
    return {'InventoryID': sku, 'QtyRequired': qty, 'BatchSize': batch, 'UOM': uom, **extra}


def bom(parent, materials, bom_id=None, revision='A', start='2024-01-01T00:00:00', end=None):
    return {'BOMID': bom_id or f'BOM-{parent}', 'Revision': revision, 'InventoryID': parent, 'Status': 'Active',
            'EffStartDate': start, 'EffEndDate': end, 'Operations': [{'OperationNbr': '10', 'Material': materials}]}


def base_records():
    return {
        'BillOfMaterial': [
            bom('FG-1', [material('SA-1', 2), material('C-BOLT', 1, uom='BOX'), material('C-OIL', 500, uom='ML', batch=1000)]),
            bom('SA-1', [material('C-BOLT', 4), material('C-PLATE', 1)]),
        ],
        'StockItem': [
            item('FG-1', warehouses=[{'WarehouseID': 'MAIN', 'QtyOnHand': 10}]),
            item('SA-1', warehouses=[{'WarehouseID': 'MAIN', 'QtyOnHand': 3}]),
            item('C-BOLT', conversions=[conv('BOX', 100)],
                 warehouses=[{'WarehouseID': 'MAIN', 'QtyOnHand': 250}, {'WarehouseID': 'WEST', 'QtyOnHand': 50}],
                 vendors=[{'VendorID': 'V1', 'Default': True, 'Active': True, 'Warehouse': None,
                           'LeadTimeDays': 14, 'AddLeadTimeDays': 2, 'LastVendorPrice': 12.0, 'LotSize': 5,
                           'PurchaseUnit': 'BOX'},
                          {'VendorID': 'V1', 'Default': False, 'Active': True, 'Warehouse': 'WEST',
                           'LeadTimeDays': 99, 'LastVendorPrice': 1, 'PurchaseUnit': 'EACH'},
                          {'VendorID': 'V2', 'Default': False, 'Active': False, 'LeadTimeDays': 7}]),
            item('C-PLATE', last_cost=3.5,
                 vendors=[{'VendorID': 'V2', 'Default': True, 'Active': True, 'LeadTimeDays': None,
                           'LastVendorPrice': 0, 'PurchaseUnit': 'EACH'}]),
            item('C-OIL', base='L', conversions=[conv('ML', 1000, op='Divide', to='L')]),
            item('UNRELATED'),
        ],
        'Vendor': [{'VendorID': 'V1', 'VendorName': 'Bolt Co', 'LeadTimedays': None},
                   {'VendorID': 'V2', 'VendorName': 'Plate Co', 'LeadTimedays': 30}],
        'PurchaseOrder': [
            {'OrderNbr': 'PO1', 'Type': 'Normal', 'Status': 'Open', 'Date': '2026-09-01T00:00:00-07:00', 'VendorID': 'V1',
             'Details': [
                 {'LineNbr': 1, 'LineType': 'Goods for IN', 'InventoryID': 'C-BOLT', 'UOM': 'BOX', 'OrderQty': 3,
                  'QtyOnReceipts': 1, 'UnitCost': 12, 'Promised': '2026-11-15T00:00:00', 'Completed': False,
                  'Cancelled': False},
                 {'LineNbr': 2, 'LineType': 'Non-Stock', 'InventoryID': 'C-PLATE', 'UOM': 'EACH', 'OrderQty': 9},
                 {'LineNbr': 3, 'LineType': 'Goods for IN', 'InventoryID': 'C-PLATE', 'UOM': 'CRATE', 'OrderQty': 9},
             ]},
            {'OrderNbr': 'PO0', 'Type': 'Normal', 'Status': 'Closed', 'Date': '2026-03-01T00:00:00', 'VendorID': 'V2',
             'Details': [{'LineNbr': 1, 'LineType': 'Goods for IN', 'InventoryID': 'C-PLATE', 'UOM': 'EACH',
                          'OrderQty': 40, 'QtyOnReceipts': 40, 'UnitCost': 3.25, 'Completed': True,
                          'Cancelled': False}]},
        ],
        'Shipment': [
            {'ShipmentNbr': 'S1', 'ShipmentDate': '2026-09-10T00:00:00', 'Status': 'Completed',
             'Details': [{'InventoryID': 'FG-1', 'ShippedQty': 6, 'UOM': 'EACH'},
                         {'InventoryID': 'C-BOLT', 'ShippedQty': 999, 'UOM': 'EACH'}]},
        ],
        'SalesOrder': [
            {'OrderType': 'SO', 'OrderNbr': 'SO1', 'Status': 'Open', 'RequestedOn': '2026-11-20T00:00:00',
             'Details': [{'InventoryID': 'FG-1', 'OpenQty': 4, 'UOM': 'EACH', 'Operation': 'Issue', 'Completed': False,
                          'RequestedOn': None},
                         {'InventoryID': 'FG-1', 'OpenQty': 2, 'UOM': 'EACH', 'Operation': 'Receipt'}]},
        ],
    }


def run(records=None, sources=('trailing_shipments',), deny=()):
    client = FakeClient(records or base_records(), deny=deny)
    return fetch_all(client, FetchOptions(today=TODAY, demand_sources=list(sources))), client


class UnitsTests(unittest.TestCase):
    def test_conversions(self):
        u = Units()
        u.add_item('A', 'EACH', [conv('BOX', 12), conv('PACK', 2, op='Divide'), conv('BOX', 5, to='KG')])
        self.assertEqual(u.qty('A', 2, 'BOX'), D(24))
        self.assertEqual(u.qty('A', 3, 'each'), D(3))
        self.assertEqual(u.qty('A', 4, 'PACK'), D(2))
        self.assertEqual(u.qty('A', 1, None), D(1))
        self.assertIsNone(u.qty('A', 1, 'PALLET'))
        self.assertIsNone(u.qty('UNKNOWN', 1, 'EACH'))
        self.assertEqual(u.unit_cost('A', 24, 'BOX'), D(2))


class ExplodeTests(unittest.TestCase):
    def test_multi_level(self):
        direct = {'FG': [('SA', D(2)), ('C1', D(1))], 'SA': [('C1', D(3)), ('C2', D('0.5'))]}
        out = explode(direct, [])
        self.assertEqual(out['FG'], {'C1': D(7), 'C2': D(1)})
        self.assertEqual(out['SA'], {'C1': D(3), 'C2': D('0.5')})

    def test_loop_is_reported(self):
        warnings = []
        explode({'A': [('B', D(1))], 'B': [('A', D(1))]}, warnings)
        self.assertTrue(any('loop' in w for w in warnings))


class FetchAllTests(unittest.TestCase):
    def test_bom_exploded_and_converted(self):
        result, _ = run()
        bom = {(r['fg_sku'], r['component_sku']): r['qty_per'] for r in result.data['bom']}
        # FG-1: 2 x SA-1 (4 bolts + 1 plate each) + 1 BOX of bolts (=100) + 500 ML oil per 1000 batch = 0.5 ML = 0.0005 L
        self.assertEqual(bom[('FG-1', 'C-BOLT')], D(108))
        self.assertEqual(bom[('FG-1', 'C-PLATE')], D(2))
        self.assertEqual(bom[('FG-1', 'C-OIL')], D('0.0005'))
        self.assertEqual(bom[('SA-1', 'C-BOLT')], D(4))
        self.assertNotIn(('FG-1', 'SA-1'), bom)

    def test_kinds_and_scope(self):
        result, _ = run()
        kinds = {r['sku']: r['kind'] for r in result.data['stock_items']}
        self.assertEqual(kinds, {'FG-1': 'finished_good', 'SA-1': 'finished_good', 'C-BOLT': 'component',
                                 'C-PLATE': 'component', 'C-OIL': 'component'})

    def test_on_hand(self):
        result, _ = run()
        bolts = {r['warehouse']: r['qty'] for r in result.data['on_hand'] if r['sku'] == 'C-BOLT'}
        self.assertEqual(bolts, {'MAIN': D(250), 'WEST': D(50)})

    def test_item_vendors(self):
        result, _ = run()
        iv = {(r['sku'], r['vendor_id']): r for r in result.data['item_vendors']}
        bolt = iv[('C-BOLT', 'V1')]
        self.assertEqual(bolt['lead_time_days'], 16)          # 14 + 2 additional
        self.assertEqual(bolt['last_cost'], D('0.12'))        # $12 per BOX of 100
        self.assertEqual(bolt['lot_size'], D(500))            # 5 BOX
        self.assertTrue(bolt['is_default'])
        self.assertNotIn(('C-BOLT', 'V2'), iv)                # inactive vendor line
        plate = iv[('C-PLATE', 'V2')]
        self.assertIsNone(plate['lead_time_days'])            # falls back to vendor lead time in planning
        self.assertEqual(plate['last_cost'], D('3.5'))        # no vendor price -> item last cost

    def test_purchase_orders(self):
        result, _ = run()
        self.assertEqual(result.data['open_po_lines'], [{
            'order_nbr': 'PO1', 'line_nbr': 1, 'sku': 'C-BOLT', 'vendor_id': 'V1', 'qty_open': D(200),
            'promised_date': dt.date(2026, 11, 15)}])
        history = {(r['order_nbr'], r['sku']): r for r in result.data['po_history']}
        self.assertEqual(history[('PO1', 'C-BOLT')]['qty'], D(300))
        self.assertEqual(history[('PO1', 'C-BOLT')]['unit_cost'], D('0.12'))
        self.assertEqual(history[('PO0', 'C-PLATE')]['order_date'], dt.date(2026, 3, 1))
        self.assertTrue(any('CRATE' in w and 'PO1' in w for w in result.warnings))

    def test_purchase_order_filter_is_normal_type_only(self):
        _, client = run()
        po_filter = next(f for _, entity, f in client.calls if entity == 'PurchaseOrder')
        self.assertTrue(po_filter.startswith("Type eq 'Normal' and ("))
        self.assertIn("Date ge datetimeoffset'2024-10-01T00:00:00Z'", po_filter)

    def test_shipment_demand_only_for_bom_parents(self):
        result, client = run()
        self.assertEqual(result.data['demand'], [
            {'sku': 'FG-1', 'source': 'trailing_shipments', 'month': dt.date(2026, 9, 10), 'qty': D(6)}])
        ship_filter = next(f for _, entity, f in client.calls if entity == 'Shipment')
        self.assertIn("Operation eq 'Issue'", ship_filter)

    def test_open_sales_orders(self):
        result, client = run(sources=('open_sales_orders',))
        self.assertEqual(result.data['demand'], [
            {'sku': 'FG-1', 'source': 'open_sales_orders', 'month': dt.date(2026, 11, 20), 'qty': D(4)}])
        so_filter = next(f for _, entity, f in client.calls if entity == 'SalesOrder')
        self.assertIn("OrderType eq 'SO'", so_filter)

    def test_forecast_not_available(self):
        with self.assertRaisesRegex(AcumaticaError, 'Forecast'):
            run(sources=('forecast',))

    def test_bom_permission_error_explains_fix(self):
        with self.assertRaisesRegex(AcumaticaPermissionError, 'AM208000'):
            run(deny={'BillOfMaterial'})

    def test_choose_latest_effective_bom(self):
        records = base_records()
        records['BillOfMaterial'] = [
            bom('FG-1', [material('C-PLATE', 1)], bom_id='OLD', start='2023-01-01T00:00:00'),
            bom('FG-1', [material('C-PLATE', 2)], bom_id='NEW', start='2025-01-01T00:00:00'),
            bom('FG-1', [material('C-PLATE', 9)], bom_id='FUTURE', start='2027-01-01T00:00:00'),
            bom('FG-1', [material('C-PLATE', 9)], bom_id='ENDED', start='2025-06-01T00:00:00', end='2026-01-01T00:00:00'),
        ]
        result, _ = run(records)
        self.assertEqual(result.data['bom'], [{'fg_sku': 'FG-1', 'component_sku': 'C-PLATE', 'qty_per': D(2)}])
        self.assertTrue(any('2 active BOMs' in w for w in result.warnings))

    def test_inactive_bom_items_skipped_with_warning(self):
        records = base_records()
        records['BillOfMaterial'][1]['Operations'][0]['Material'].append(material('GONE', 1))
        result, _ = run(records)
        self.assertNotIn('GONE', {r['component_sku'] for r in result.data['bom']})
        self.assertTrue(any('GONE' in w for w in result.warnings))

    def test_months_ago(self):
        self.assertEqual(entities.months_ago(dt.date(2026, 2, 14), 3), dt.date(2025, 11, 1))


if __name__ == '__main__':
    unittest.main()
