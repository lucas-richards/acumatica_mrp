import collections
import datetime as dt
import unittest
from decimal import Decimal as D

from acumatica import entities
from acumatica.entities import FetchOptions, Units, explode, fetch_all
from acumatica.rest import AcumaticaError, AcumaticaPermissionError, AcumaticaRequestError

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

    def generic_inquiry(self, name, select=None):
        self.calls.append(('odata', name, ''))
        if name in self.deny or name not in self.records:
            raise AcumaticaRequestError(f"GI {name!r} not found (is it exposed via OData?)", 404)
        return self.records[name]


def item(sku, base='EACH', conversions=(), warehouses=(), vendors=(), **extra):
    return {'InventoryID': sku, 'Description': f'{sku} desc', 'ItemClass': 'CLS', 'BaseUOM': base,
            'UOMConversions': list(conversions), 'WarehouseDetails': list(warehouses),
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
                           'LeadTimeDays': 14, 'AddLeadTimeDays': 2, 'LotSize': 5,
                           'PurchaseUnit': 'BOX'},
                          {'VendorID': 'V1', 'Default': False, 'Active': True, 'Warehouse': 'WEST',
                           'LeadTimeDays': 99, 'PurchaseUnit': 'EACH'},
                          {'VendorID': 'V2', 'Default': False, 'Active': False, 'LeadTimeDays': 7}]),
            item('C-PLATE',
                 vendors=[{'VendorID': 'V2', 'Default': True, 'Active': True, 'LeadTimeDays': None,
                           'PurchaseUnit': 'EACH'}]),
            item('C-OIL', base='L', conversions=[conv('ML', 1000, op='Divide', to='L')]),
            item('UNRELATED'),
        ],
        'Vendor': [{'VendorID': 'V1', 'VendorName': 'Bolt Co', 'LeadTimedays': None},
                   {'VendorID': 'V2', 'VendorName': 'Plate Co', 'LeadTimedays': 30}],
        'PurchaseOrder': [
            {'OrderNbr': 'PO1', 'Type': 'Normal', 'Status': 'Open', 'Date': '2026-09-01T00:00:00-07:00', 'VendorID': 'V1',
             'Details': [
                 {'LineNbr': 1, 'LineType': 'Goods for IN', 'InventoryID': 'C-BOLT', 'UOM': 'BOX', 'OrderQty': 3,
                  'QtyOnReceipts': 1, 'Promised': '2026-11-15T00:00:00', 'Completed': False,
                  'Cancelled': False},
                 {'LineNbr': 2, 'LineType': 'Non-Stock', 'InventoryID': 'C-PLATE', 'UOM': 'EACH', 'OrderQty': 9},
                 {'LineNbr': 3, 'LineType': 'Goods for IN', 'InventoryID': 'C-PLATE', 'UOM': 'CRATE', 'OrderQty': 9},
             ]},
            {'OrderNbr': 'PO0', 'Type': 'Normal', 'Status': 'Closed', 'Date': '2026-03-01T00:00:00', 'VendorID': 'V2',
             'Details': [{'LineNbr': 1, 'LineType': 'Goods for IN', 'InventoryID': 'C-PLATE', 'UOM': 'EACH',
                          'OrderQty': 40, 'QtyOnReceipts': 40, 'Completed': True,
                          'Cancelled': False}]},
            {'OrderNbr': 'PO9', 'Type': 'Normal', 'Status': 'Completed', 'Date': '2026-09-20T00:00:00', 'VendorID': 'V2',
             'Details': [{'LineNbr': 1, 'LineType': 'Goods for IN', 'InventoryID': 'C-PLATE', 'UOM': 'EACH',
                          'OrderQty': 50, 'QtyOnReceipts': 0, 'Completed': True, 'Cancelled': False}]},
        ],
        'SalesInvoice': [
            {'ReferenceNbr': 'I1', 'Type': 'Invoice', 'Date': '2026-09-10T00:00:00', 'Status': 'Closed',
             'Details': [{'InventoryID': 'FG-1', 'Qty': 8, 'UOM': 'EACH'}, {'InventoryID': 'C-BOLT', 'Qty': 5, 'UOM': 'EACH'}]},
            {'ReferenceNbr': 'CM1', 'Type': 'Credit Memo', 'Date': '2026-09-12T00:00:00', 'Status': 'Open',
             'Details': [{'InventoryID': 'FG-1', 'Qty': 2, 'UOM': 'EACH'}]},
        ],
        'InventoryIssue': [
            {'ReferenceNbr': 'IS1', 'Date': '2026-09-10T00:00:00', 'Status': 'Released',
             'Details': [{'InventoryID': 'C-BOLT', 'Qty': 999, 'UOM': 'EACH', 'TranType': 'Issue'},
                        {'InventoryID': 'C-BOLT', 'Qty': 98000, 'UOM': 'EACH', 'TranType': 'Invoice'},
                         {'InventoryID': 'C-BOLT', 'Qty': 9, 'UOM': 'EACH', 'TranType': 'Credit Memo'},
                         {'InventoryID': 'FG-1', 'Qty': 3, 'UOM': 'EACH', 'TranType': 'Issue'},
                         {'InventoryID': 'UNRELATED', 'Qty': 4, 'UOM': 'EACH', 'TranType': 'Issue'}]},
        ],
        'KitAssembly': [
            {'ReferenceNbr': 'K1', 'Type': 'Assembly', 'Status': 'Released', 'Date': '2026-09-15T00:00:00',
             'KitInventoryID': 'FG-1', 'Qty': 3, 'UOM': 'EACH',
             'StockComponents': [{'StockInventoryID': 'C-PLATE', 'Qty': 30, 'UOM': 'EACH'},
                                 {'StockInventoryID': 'SA-1', 'Qty': 3, 'UOM': 'EACH'}]},
            {'ReferenceNbr': 'K2', 'Type': 'Disassembly', 'Status': 'Released', 'Date': '2026-09-20T00:00:00',
             'KitInventoryID': 'SA-1', 'Qty': 1, 'UOM': 'EACH',
             'StockComponents': [{'StockInventoryID': 'C-PLATE', 'Qty': 5, 'UOM': 'EACH'}]},
        ],
        'ProductionOrder': [
            {'id': 'p1', 'ProductionNbr': 'WO1', 'InventoryID': 'FG-1', 'Status': 'Released', 'QtyRemaining': 12,
             'UOM': 'EACH', 'EndDate': '2026-11-10T00:00:00'},
            {'id': 'p0', 'ProductionNbr': 'WO0', 'InventoryID': 'FG-1', 'Status': 'Completed', 'QtytoProduce': 50,
             'UOM': 'EACH', 'EndDate': '2026-08-10T00:00:00'},
            {'id': 'p00', 'ProductionNbr': 'WO00', 'InventoryID': 'FG-1', 'Status': 'Closed', 'QtytoProduce': 30,
             'UOM': 'EACH', 'EndDate': '2026-05-10T00:00:00'},
        ],
        'ProductionOrderDetail': [
            {'id': 'p1', 'ProductionNbr': 'WO1', 'Operations': [{'Material': [
                {'InventoryID': 'C-PLATE', 'QtyRemaining': 24, 'UOM': 'EACH', 'Byproduct': False},
                {'InventoryID': 'C-BOLT', 'QtyRemaining': 7, 'UOM': 'EACH', 'Byproduct': True},
                {'InventoryID': 'NON-STOCK', 'QtyRemaining': 9, 'UOM': 'EACH'}]}]},
        ],
        'SalesOrder': [
            {'OrderType': 'SO', 'OrderNbr': 'SO1', 'Status': 'Open', 'RequestedOn': '2026-11-20T00:00:00',
             'Details': [{'InventoryID': 'FG-1', 'OpenQty': 4, 'UOM': 'EACH', 'Operation': 'Issue', 'Completed': False,
                          'RequestedOn': None},
                         {'InventoryID': 'FG-1', 'OpenQty': 2, 'UOM': 'EACH', 'Operation': 'Receipt'}]},
        ],
    }


def run(records=None, sources=('trailing_invoices',), deny=()):
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

    def test_compounding_skus_excluded(self):
        records = base_records()
        records['BillOfMaterial'][0]['Operations'][0]['Material'].append(material('XLDXRX-001', 1))
        records['BillOfMaterial'].append(bom('XLDXRX-FG', [material('C-PLATE', 1)]))
        records['StockItem'] += [item('XLDXRX-001'), item('XLDXRX-FG')]
        result, _ = run(records)
        skus = {r['sku'] for r in result.data['stock_items']}
        self.assertFalse({s for s in skus if s.startswith('XLDXRX')})
        self.assertFalse([r for r in result.data['bom'] if 'XLDXRX' in r['fg_sku'] + r['component_sku']])

    def test_on_hand(self):
        result, _ = run()
        bolts = {r['warehouse']: r['qty'] for r in result.data['on_hand'] if r['sku'] == 'C-BOLT'}
        self.assertEqual(bolts, {'MAIN': D(250), 'WEST': D(50)})

    def test_item_vendors(self):
        result, _ = run()
        iv = {(r['sku'], r['vendor_id']): r for r in result.data['item_vendors']}
        bolt = iv[('C-BOLT', 'V1')]
        self.assertEqual(bolt['lead_time_days'], 16)          # 14 + 2 additional
        self.assertEqual(bolt['lot_size'], D(500))            # 5 BOX
        self.assertTrue(bolt['is_default'])
        self.assertNotIn(('C-BOLT', 'V2'), iv)                # inactive vendor line
        plate = iv[('C-PLATE', 'V2')]
        self.assertIsNone(plate['lead_time_days'])            # falls back to vendor lead time in planning

    def test_purchase_orders(self):
        result, _ = run()
        self.assertEqual(result.data['open_po_lines'], [{
            'order_nbr': 'PO1', 'line_nbr': 1, 'sku': 'C-BOLT', 'vendor_id': 'V1', 'qty_open': D(200),
            'promised_date': dt.date(2026, 11, 15), 'order_date': dt.date(2026, 9, 1)}])
        history = {(r['order_nbr'], r['sku']): r for r in result.data['po_history']}
        self.assertEqual(history[('PO1', 'C-BOLT')]['qty'], D(300))
        self.assertEqual(history[('PO0', 'C-PLATE')]['order_date'], dt.date(2026, 3, 1))
        self.assertNotIn(('PO9', 'C-PLATE'), history)    # closed with nothing received: dropped, not a purchase
        self.assertTrue(any('CRATE' in w and 'PO1' in w for w in result.warnings))

    def test_purchase_order_filter_is_normal_type_only(self):
        _, client = run()
        po_filter = next(f for _, entity, f in client.calls if entity == 'PurchaseOrder')
        self.assertTrue(po_filter.startswith("Type eq 'Normal' and ("))
        self.assertIn("Date ge datetimeoffset'2024-10-01T00:00:00Z'", po_filter)

    def test_invoice_demand_for_finished_goods(self):
        result, client = run()
        fg = [r for r in result.data['demand'] if r['source'] == 'trailing_invoices']
        self.assertEqual({r['sku'] for r in fg}, {'FG-1'})           # components are not finished-good demand
        self.assertEqual(sum(r['qty'] for r in fg), D(6))            # 8 invoiced - 2 credit memo
        invoice_filter = next(f for _, entity, f in client.calls if entity == 'SalesInvoice')
        self.assertIn("(Status eq 'Open' or Status eq 'Closed')", invoice_filter)

    def test_issue_history_for_components(self):
        result, client = run()
        issues = [r for r in result.data['demand'] if r['source'] == 'trailing_issues']
        self.assertEqual({r['sku'] for r in issues}, {'C-BOLT'})     # not FG-1, not UNRELATED
        self.assertEqual(sum(r['qty'] for r in issues), D(999))      # Issue lines only: no direct sales, no returns
        issue_filter = next(f for _, entity, f in client.calls if entity == 'InventoryIssue')
        self.assertIn("Status eq 'Released'", issue_filter)

    def test_assembly_out(self):
        result, _ = run()
        rows = [r for r in result.data['demand'] if r['source'] == 'trailing_assembly']
        by_sku = collections.defaultdict(D)
        for r in rows:
            by_sku[r['sku']] += r['qty']
        # assembly consumes 30 plates + 3 subassemblies; disassembly returns 5 plates and consumes the kit (SA-1)
        self.assertEqual(dict(by_sku), {'C-PLATE': D(25), 'SA-1': D(4)})

    def test_open_orders_for_so_po_column(self):
        result, _ = run(sources=('trailing_invoices',))
        so = [r for r in result.data['demand'] if r['source'] == 'open_so']
        po = [r for r in result.data['demand'] if r['source'] == 'open_po']
        self.assertEqual(sum(r['qty'] for r in so if r['sku'] == 'FG-1'), D(4))
        by_sku = collections.defaultdict(D)
        for r in po:
            by_sku[r['sku']] += r['qty']
        # 12 FG-1 still to produce; plates still to issue; no by-products or non-stock materials
        self.assertEqual(dict(by_sku), {'FG-1': D(12), 'C-PLATE': D(24)})

    def test_open_sales_orders(self):
        result, client = run(sources=('open_sales_orders',))
        fg = [r for r in result.data['demand'] if r['source'] == 'open_sales_orders']
        self.assertEqual(fg, [
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

    def test_available_quantity_from_gi(self):
        records = base_records()
        records['LR-Available Qty'] = [
            {'SKU': 'C-BOLT  ', 'Warehouse': 'MAIN ', 'Available': '120.000000'},
            {'SKU': 'C-BOLT  ', 'Warehouse': 'MAIN ', 'Available': '120.000000'},   # repeated join row
            {'SKU': 'C-BOLT', 'Warehouse': 'WEST', 'Available': '-20.000000'},       # back-ordered
            {'SKU': 'FG-1', 'Warehouse': 'MAIN', 'Available': '4'},
        ]
        result, _ = run(records)
        avail = {r['sku']: r['qty_available'] for r in result.data['stock_items']}
        self.assertEqual(avail['C-BOLT'], D(100))
        self.assertEqual(avail['FG-1'], D(4))
        self.assertEqual(avail['C-PLATE'], D(0))
        self.assertFalse(any('Available quantity' in w for w in result.warnings))

    def test_available_quantity_unreadable_falls_back(self):
        result, _ = run()
        self.assertTrue(all(r['qty_available'] is None for r in result.data['stock_items']))
        self.assertTrue(any("GI 'LR-Available Qty'" in w for w in result.warnings))

    def test_open_po_order_date(self):
        result, _ = run()
        self.assertEqual(result.data['open_po_lines'][0]['order_date'], dt.date(2026, 9, 1))

    def test_item_type_and_replenishment_source(self):
        records = base_records()
        for item in records['StockItem']:
            if item['InventoryID'] == 'C-PLATE':
                item['ReplenishmentParameters'] = [{'Source': 'Manufacturing'}]
            if item['InventoryID'] == 'C-BOLT':
                item['ReplenishmentParameters'] = [{'Source': 'Purchase'}, {'Source': 'Manufacturing'}]
            if item['InventoryID'] == 'SA-1':
                item['ItemType'] = 'Subassembly'
        result, _ = run(records)
        rows = {r['sku']: r for r in result.data['stock_items']}
        self.assertEqual(rows['C-PLATE']['replenishment_source'], 'Manufacturing')
        self.assertEqual(rows['C-BOLT']['replenishment_source'], 'Mixed')
        self.assertEqual(rows['C-OIL']['replenishment_source'], '')
        self.assertEqual(rows['SA-1']['item_type'], 'Subassembly')

    def test_purchased_finished_good_gets_item_vendors(self):
        records = base_records()
        for item in records['StockItem']:
            if item['InventoryID'] == 'FG-1':
                item['ReplenishmentParameters'] = [{'Source': 'Purchase'}]
                item['VendorDetails'] = [{'VendorID': 'V1', 'Default': True, 'Active': True, 'LeadTimeDays': 20}]
        result, _ = run(records)
        iv = {(r['sku'], r['vendor_id']): r for r in result.data['item_vendors']}
        self.assertEqual(iv[('FG-1', 'V1')]['lead_time_days'], 20)
        # a finished good that is built gets none
        records = base_records()
        for item in records['StockItem']:
            if item['InventoryID'] == 'FG-1':
                item['VendorDetails'] = [{'VendorID': 'V1', 'Default': True, 'Active': True, 'LeadTimeDays': 20}]
        result, _ = run(records)
        self.assertNotIn('FG-1', {r['sku'] for r in result.data['item_vendors']})

    def test_bought_item_in_no_bom_is_kept(self):
        records = base_records()
        records['StockItem'].append(item('KIT-BOX', vendors=[
            {'VendorID': 'V1', 'Default': True, 'Active': True, 'LeadTimeDays': 30}]))
        records['StockItem'].append(item('NO-VENDOR'))
        made = item('MADE', vendors=[{'VendorID': 'V1', 'Default': True, 'Active': True}])
        made['ReplenishmentParameters'] = [{'Source': 'Manufacturing'}]
        records['StockItem'].append(made)
        result, _ = run(records)
        rows = {r['sku']: r for r in result.data['stock_items']}
        self.assertEqual(rows['KIT-BOX']['kind'], 'finished_good')
        self.assertNotIn('NO-VENDOR', rows)
        self.assertNotIn('MADE', rows)
        iv = {(r['sku'], r['vendor_id']) for r in result.data['item_vendors']}
        self.assertIn(('KIT-BOX', 'V1'), iv)

    def test_quantity_in_receiving(self):
        records = base_records()
        gi = 'DB-StorageDetailsByItemWarehouseLocation'
        records[gi] = [
            {'InventoryID': 'C-BOLT  ', 'WarehouseID': 'MAIN', 'LocationID': 'REC-INS   ', 'QtyOnHandinLocation': '40.000000'},
            {'InventoryID': 'C-BOLT  ', 'WarehouseID': 'MAIN', 'LocationID': 'REC-INS   ', 'QtyOnHandinLocation': '40.000000'},  # repeated join row
            {'InventoryID': 'C-BOLT  ', 'WarehouseID': 'WEST', 'LocationID': 'REC-INS', 'QtyOnHandinLocation': '10'},
            {'InventoryID': 'C-BOLT  ', 'WarehouseID': 'MAIN', 'LocationID': 'FG-01', 'QtyOnHandinLocation': '999'},
            {'InventoryID': 'C-PLATE', 'WarehouseID': 'MAIN', 'LocationID': 'REC-INS', 'QtyOnHandinLocation': '0'},
        ]
        result, _ = run(records)
        rows = {r['sku']: r for r in result.data['stock_items']}
        self.assertEqual(rows['C-BOLT']['qty_receiving'], D(50))      # two warehouses, repeats counted once
        self.assertEqual(rows['C-PLATE']['qty_receiving'], D(0))
        # GI unreadable: nothing is counted and the sync says so
        result, _ = run(base_records())
        self.assertTrue(all(r['qty_receiving'] is None for r in result.data['stock_items']))
        self.assertTrue(any('in receiving' in w.lower() for w in result.warnings))

    def test_source_comes_from_the_inventory_planning_warehouse_row(self):
        warehouse = lambda source: {'WarehouseID': 'MAIN', 'QtyOnHand': 1, 'ReplenishmentSource': source}
        # the class says Purchase, the planning tab says Manufacturing: the tab wins
        made = {'WarehouseDetails': [warehouse('Manufacturing')], 'ReplenishmentParameters': [{'Source': 'Purchase'}]}
        self.assertEqual(entities.replenishment_source(made), 'Manufacturing')
        kit = {'WarehouseDetails': [warehouse('Kit Assembly')], 'ReplenishmentParameters': [{'Source': 'Manufacturing'}]}
        self.assertEqual(entities.replenishment_source(kit), 'Kit Assembly')
        both = {'WarehouseDetails': [warehouse('Purchase'), warehouse('Manufacturing')]}
        self.assertEqual(entities.replenishment_source(both), 'Mixed')
        # no warehouse source: fall back to the class
        self.assertEqual(entities.replenishment_source(
            {'WarehouseDetails': [warehouse(None)], 'ReplenishmentParameters': [{'Source': 'Purchase'}]}), 'Purchase')
        self.assertEqual(entities.replenishment_source({}), '')

    def test_planning_method_and_source_come_from_the_stock_item_inquiry(self):
        records = base_records()
        records['IN-StockItem'] = [
            {'InventoryID': 'C-BOLT   ', 'PlanningMethod': 'MRP', 'Source': 'Purchase'},
            {'InventoryID': 'C-PLATE', 'PlanningMethod': 'None', 'Source': 'Manufacturing'},
            {'InventoryID': 'SA-1', 'PlanningMethod': 'MRP', 'Source': 'Kit Assembly'},
        ]
        for it in records['StockItem']:
            if it['InventoryID'] == 'C-PLATE':   # the API says Purchase; the inquiry (the screen) says Manufacturing
                it['ReplenishmentParameters'] = [{'Source': 'Purchase', 'Method': 'None'}]
        result, _ = run(records)
        rows = {r['sku']: r for r in result.data['stock_items']}
        self.assertEqual((rows['C-BOLT']['planning_method'], rows['C-BOLT']['replenishment_source']), ('MRP', 'Purchase'))
        self.assertEqual((rows['C-PLATE']['planning_method'], rows['C-PLATE']['replenishment_source']), ('None', 'Manufacturing'))
        self.assertEqual(rows['SA-1']['replenishment_source'], 'Kit Assembly')
        # an item the inquiry does not list keeps the API source and has no planning method
        self.assertEqual(rows['C-OIL']['planning_method'], '')

    def test_inquiry_unreadable_falls_back_to_the_api_source(self):
        records = base_records()
        for it in records['StockItem']:
            if it['InventoryID'] == 'C-PLATE':
                it['ReplenishmentParameters'] = [{'Source': 'Manufacturing', 'Method': 'None'}]
        result, _ = run(records)
        rows = {r['sku']: r for r in result.data['stock_items']}
        self.assertEqual(rows['C-PLATE']['replenishment_source'], 'Manufacturing')
        self.assertTrue(all(r['planning_method'] == '' for r in rows.values()))
        self.assertTrue(any('IN-StockItem' in w for w in result.warnings))

    def test_last_production_run(self):
        result, _ = run()
        self.assertEqual(result.data['last_production'],
                         [{'sku': 'FG-1', 'qty': D(50), 'run_date': dt.date(2026, 8, 10)}])

    def test_months_ago(self):
        self.assertEqual(entities.months_ago(dt.date(2026, 2, 14), 3), dt.date(2025, 11, 1))


if __name__ == '__main__':
    unittest.main()
