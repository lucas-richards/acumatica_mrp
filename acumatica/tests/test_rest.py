import unittest
from unittest import mock

import requests

from acumatica.rest import (AcumaticaConfig, AcumaticaConfigError, AcumaticaPermissionError, AcumaticaRequestError,
                            RestClient, flatten)

CONFIG = AcumaticaConfig(base_url='https://erp.example.com/', tenant='Westridge', username='svc', password='pw',
                         branch='WLI', page_size=2)


def response(status=200, payload=None):
    r = mock.Mock(status_code=status, text=str(payload))
    r.json.return_value = payload
    return r


def wrap(**fields):
    return {k: {'value': v} for k, v in fields.items()}


class FlattenTests(unittest.TestCase):
    def test_flatten_nested(self):
        raw = {'id': 'x', 'rowNumber': 1, 'note': {'value': ''}, '_links': {}, 'custom': {},
               'InventoryID': {'value': 'A'}, 'Missing': {},
               'Details': [{'Qty': {'value': 2.5}, '_links': {}}]}
        self.assertEqual(flatten(raw), {'id': 'x', 'InventoryID': 'A', 'Missing': None, 'Details': [{'Qty': 2.5}]})


class ClientTests(unittest.TestCase):
    def make(self, get_responses, post_status=204):
        session = mock.Mock(spec=requests.Session)
        session.headers = {}
        session.post.return_value = response(post_status, {'message': 'bad login'})
        session.get.side_effect = list(get_responses)
        return RestClient(CONFIG, session=session), session

    def test_missing_config(self):
        with self.assertRaisesRegex(AcumaticaConfigError, 'ACUMATICA_PASSWORD'):
            RestClient(AcumaticaConfig(base_url='x', tenant='t', username='u', password=''))

    def test_login_fetch_logout(self):
        client, session = self.make([
            response(payload=[wrap(InventoryID='A'), wrap(InventoryID='B')]),
            response(payload=[wrap(InventoryID='C')]),
        ])
        with client as c:
            rows = c.fetch('default', 'StockItem', select=['InventoryID'], filter="ItemStatus ne 'Inactive'")
        self.assertEqual([r['InventoryID'] for r in rows], ['A', 'B', 'C'])
        login, logout = session.post.call_args_list
        self.assertTrue(login.args[0].endswith('/entity/auth/login'))
        self.assertEqual(login.kwargs['json'], {'name': 'svc', 'password': 'pw', 'company': 'Westridge', 'branch': 'WLI'})
        self.assertTrue(logout.args[0].endswith('/entity/auth/logout'))
        first, second = session.get.call_args_list
        self.assertEqual(first.args[0], 'https://erp.example.com/entity/Default/24.200.001/StockItem')
        self.assertEqual(second.kwargs['params']['$skip'], '2')
        self.assertEqual(first.kwargs['params']['$filter'], "ItemStatus ne 'Inactive'")

    def test_only_reads(self):
        client, session = self.make([response(payload=[])])
        with client as c:
            c.fetch('manufacturing', 'BillOfMaterial')
        self.assertFalse(session.put.called)   # only GET reads
        self.assertFalse(session.delete.called)
        self.assertEqual(session.post.call_count, 2)   # login + logout only

    def test_logout_even_on_error(self):
        client, session = self.make([response(status=500, payload={'exceptionMessage': 'boom'})])
        with self.assertRaisesRegex(AcumaticaRequestError, 'boom'):
            with client as c:
                c.fetch('default', 'Vendor')
        self.assertTrue(session.post.call_args_list[-1].args[0].endswith('/entity/auth/logout'))

    def test_generic_inquiry_over_odata(self):
        client, session = self.make([response(payload={'value': [{'SKU': 'A', 'Available': '5'}]})])
        with client as c:
            rows = c.generic_inquiry('LR-Available Qty', select=['SKU', 'Available'])
        self.assertEqual(rows, [{'SKU': 'A', 'Available': '5'}])
        call = session.get.call_args
        self.assertEqual(call.args[0], 'https://erp.example.com/odata/Westridge/LR-Available%20Qty')
        self.assertEqual(call.kwargs['params'], {'$format': 'json', '$select': 'SKU,Available'})
        self.assertEqual(call.kwargs['auth'], ('svc', 'pw'))

    def test_generic_inquiry_not_found(self):
        client, _ = self.make([response(status=404, payload={})])
        with self.assertRaisesRegex(AcumaticaRequestError, 'exposed via OData'):
            with client as c:
                c.generic_inquiry('Nope')

    def test_permission_error(self):
        client, _ = self.make([response(status=403, payload={'message': 'You have insufficient rights'})])
        with self.assertRaises(AcumaticaPermissionError):
            with client as c:
                c.fetch('manufacturing', 'BillOfMaterial')

    def test_failed_login(self):
        client, session = self.make([], post_status=401)
        with self.assertRaisesRegex(AcumaticaRequestError, 'Login failed'):
            with client:
                pass
        self.assertEqual(session.post.call_count, 1)   # no logout without a session


if __name__ == '__main__':
    unittest.main()
