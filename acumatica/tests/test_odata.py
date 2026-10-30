import datetime as dt
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

import requests

from acumatica.gi_specs import SPECS, GIFieldError, map_rows
from acumatica.odata import AcumaticaConfig, AcumaticaConfigError, AcumaticaRequestError, ODataClient

CONFIG = AcumaticaConfig(base_url='https://erp.example.com/', tenant='Westridge Laboratories Inc.',
                         username='svc', password='pw', page_size=2)


def response(status=200, payload=None, text=''):
    r = mock.Mock(status_code=status, text=text)
    r.json.return_value = payload
    return r


class ClientTests(unittest.TestCase):
    def client(self, *responses):
        session = mock.Mock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = list(responses)
        return ODataClient(CONFIG, session=session), session

    def test_missing_config(self):
        with self.assertRaises(AcumaticaConfigError) as ctx:
            ODataClient(AcumaticaConfig(base_url='', tenant='', username='u', password=''))
        self.assertIn('ACUMATICA_PASSWORD', str(ctx.exception))

    def test_url_quotes_tenant_and_gi(self):
        client, _ = self.client()
        self.assertEqual(client.gi_url('MRP-BOM'),
                         'https://erp.example.com/odata/Westridge%20Laboratories%20Inc./MRP-BOM')

    def test_pages_until_short_page(self):
        client, session = self.client(
            response(payload={'value': [{'a': 1}, {'a': 2}]}),
            response(payload={'value': [{'a': 3}]}),
        )
        rows = client.fetch_rows('GI', select=['a'])
        self.assertEqual(rows, [{'a': 1}, {'a': 2}, {'a': 3}])
        self.assertEqual(session.get.call_count, 2)
        second = session.get.call_args_list[1].kwargs['params']
        self.assertEqual(second['$skip'], '2')
        self.assertEqual(second['$select'], 'a')
        self.assertEqual(second['$format'], 'json')

    def test_verbose_payload(self):
        client, _ = self.client(response(payload={'d': {'results': [{'a': 1}]}}))
        self.assertEqual(client.fetch_rows('GI'), [{'a': 1}])

    def test_auth_error(self):
        client, _ = self.client(response(status=401))
        with self.assertRaises(AcumaticaRequestError) as ctx:
            client.fetch_rows('GI')
        self.assertEqual(ctx.exception.status_code, 401)

    def test_not_found(self):
        client, _ = self.client(response(status=404))
        with self.assertRaisesRegex(AcumaticaRequestError, 'exposed via OData'):
            client.fetch_rows('GI')

    def test_network_error(self):
        client, _ = self.client(requests.ConnectionError('boom'))
        with self.assertRaisesRegex(AcumaticaRequestError, 'request failed'):
            client.fetch_rows('GI')

    def test_non_json(self):
        r = response()
        r.json.side_effect = ValueError
        client, _ = self.client(r)
        with self.assertRaisesRegex(AcumaticaRequestError, 'not JSON'):
            client.fetch_rows('GI')


class MappingTests(unittest.TestCase):
    def test_stock_items(self):
        rows = [{'InventoryID': ' COMP-1 ', 'Description': 'Bearing', 'ItemClass': 'BRG', 'IsFinishedGood': False,
                 'BaseUnit': 'EA', 'LotSize': '50.0'},
                {'InventoryID': 'FG-1', 'Description': 'Pump', 'ItemClass': 'FG', 'IsFinishedGood': 'true',
                 'BaseUnit': 'EA', 'LotSize': None}]
        out = map_rows(SPECS['stock_items'], rows)
        self.assertEqual(out[0]['sku'], 'COMP-1')
        self.assertEqual(out[0]['kind'], 'component')
        self.assertEqual(out[0]['lot_size'], D('50.0'))
        self.assertEqual(out[1]['kind'], 'finished_good')
        self.assertIsNone(out[1]['lot_size'])

    def test_dates(self):
        rows = [{'InventoryID': 'FG-1', 'ShipmentDate': '2026-09-14T00:00:00', 'ShippedQty': 3},
                {'InventoryID': 'FG-1', 'ShipmentDate': '/Date(1789776000000)/', 'ShippedQty': 2}]
        out = map_rows(SPECS['demand_shipments'], rows)
        self.assertEqual(out[0]['date'], dt.date(2026, 9, 14))
        self.assertEqual(out[1]['date'], dt.date(2026, 9, 19))

    def test_missing_column(self):
        with self.assertRaisesRegex(GIFieldError, 'QtyOnHand'):
            map_rows(SPECS['on_hand'], [{'InventoryID': 'X', 'Warehouse': 'MAIN'}])

    def test_empty_required_value(self):
        with self.assertRaisesRegex(GIFieldError, 'row 1: InventoryID is empty'):
            map_rows(SPECS['on_hand'], [{'InventoryID': '', 'Warehouse': 'MAIN', 'QtyOnHand': 1}])

    def test_bad_number(self):
        with self.assertRaisesRegex(GIFieldError, 'not a number'):
            map_rows(SPECS['on_hand'], [{'InventoryID': 'X', 'Warehouse': 'MAIN', 'QtyOnHand': 'abc'}])

    def test_empty_gi(self):
        self.assertEqual(map_rows(SPECS['bom'], []), [])


class DocumentationTests(unittest.TestCase):
    def test_admin_doc_lists_every_gi_and_column(self):
        doc = (Path(__file__).resolve().parents[2] / 'docs' / 'acumatica_generic_inquiries.md').read_text()
        for spec in SPECS.values():
            self.assertIn(spec.default_gi_name, doc, spec.key)
            for field in spec.fields:
                self.assertIn(f'`{field.gi_column}`', doc, f'{spec.key}.{field.gi_column}')


if __name__ == '__main__':
    unittest.main()
