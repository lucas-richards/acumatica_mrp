import csv
import datetime as dt
import io
import json
import tempfile
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from acumatica.odata import AcumaticaRequestError
from mrp import loaders, models, services, sync
from mrp.management.commands.seed_demo import SAMPLE_FILE, shift_sample

ANCHOR = dt.date(2026, 10, 29)


def seed():
    call_command('seed_demo', as_of=ANCHOR.isoformat(), stdout=io.StringIO())


def make_user(username, role=None, email=''):
    user = User.objects.create_user(username, email=email)   # unusable password; tests use force_login
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


@mock.patch('mrp.services.today', return_value=ANCHOR)
class PlanPageTests(TestCase):
    def setUp(self):
        seed()
        self.planner = make_user('pat', 'planner', 'pat@example.com')
        self.viewer = make_user('vic', 'viewer')

    def test_login_required(self, _today):
        response = self.client.get(reverse('mrp:plan'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_user_without_role_is_refused(self, _today):
        self.client.force_login(make_user('nobody'))
        self.assertEqual(self.client.get(reverse('mrp:plan')).status_code, 403)

    def test_plan_page_matches_mockup(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertEqual(response.status_code, 200)
        for text in ['Sample data', 'of 11 shown', '$10,195', '2 already late to order', 'COMP-2250, COMP-4408',
                     'Reminder active', 'Order by Nov 3', 'Order by Dec 14', 'Nothing to order yet',
                     '+120 in FG stock', 'Stockout risk']:
            self.assertContains(response, text)
        self.assertNotContains(response, 'Refresh from Acumatica')   # planners only
        self.assertNotContains(response, 'Create PO')

    def test_filters(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), {'vendor': 'NWMETL', 'cover': 0, 'mode': 'cover'})
        plans = response.context['result'].plans
        self.assertEqual({p.component.vendor_id for p in plans}, {'NWMETL'})
        self.assertEqual(response.context['result'].summary.components_to_order, 0)

    def test_bad_filter_values_fall_back(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), {'vendor': 'NOPE', 'cover': '9', 'mode': 'x'})
        options = response.context['options']
        self.assertEqual((options.vendor_id, options.cover_months, options.qty_mode.value), (None, 2, 'cover'))

    def test_htmx_returns_partial(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), HTTP_HX_REQUEST='true')
        self.assertNotContains(response, '<html')
        self.assertContains(response, 'Components to order')

    def test_breakdown(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:breakdown', args=['COMP-2250']))
        self.assertContains(response, 'FG-100')
        self.assertContains(response, 'FG-400')
        self.assertEqual(self.client.get(reverse('mrp:breakdown', args=['NOPE'])).status_code, 404)

    def test_empty_database_renders(self, _today):
        models.StockItem.objects.all().delete()
        models.SyncRun.objects.all().delete()
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertContains(response, 'No components to show')
        self.assertContains(response, 'No data loaded')


@mock.patch('mrp.services.today', return_value=ANCHOR)
class SettingsTests(TestCase):
    def setUp(self):
        seed()
        self.planner = make_user('pat', 'planner')
        self.viewer = make_user('vic', 'viewer')

    def test_viewer_cannot_change_settings(self, _today):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(reverse('mrp:save_reminder_days'), {'reminder_days': 14}).status_code, 403)
        self.assertEqual(self.client.post(reverse('mrp:save_defaults'), {'cover': 1, 'mode': 'cover'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('mrp:lead_times'),
                                          {'sku': 'COMP-2250', 'lead_time_days': 10}).status_code, 403)
        self.assertEqual(self.client.post(reverse('mrp:refresh')).status_code, 403)

    def test_reminder_days_change_is_logged(self, _today):
        self.client.force_login(self.planner)
        self.client.post(reverse('mrp:save_reminder_days'), {'reminder_days': 60})
        self.assertEqual(models.PlanSettings.load().reminder_days, 60)
        log = models.SettingsChangeLog.objects.get()
        self.assertEqual((log.user, log.setting, log.old_value, log.new_value), (self.planner, 'reminder_days', '30', '60'))

    def test_invalid_reminder_days(self, _today):
        self.client.force_login(self.planner)
        self.client.post(reverse('mrp:save_reminder_days'), {'reminder_days': 45})
        self.assertEqual(models.PlanSettings.load().reminder_days, 30)
        self.assertFalse(models.SettingsChangeLog.objects.exists())

    def test_save_defaults_logged(self, _today):
        self.client.force_login(self.planner)
        self.client.post(reverse('mrp:save_defaults'), {'cover': 3, 'mode': 'last_po'})
        s = models.PlanSettings.load()
        self.assertEqual((s.cover_months, s.qty_mode), (3, 'last_po'))
        self.assertEqual(set(models.SettingsChangeLog.objects.values_list('setting', flat=True)),
                         {'cover_months', 'qty_mode'})

    def test_save_defaults_rejects_out_of_range(self, _today):
        self.client.force_login(self.planner)
        self.client.post(reverse('mrp:save_defaults'), {'cover': 7, 'mode': 'cover'})
        self.assertEqual(models.PlanSettings.load().cover_months, 2)

    def test_lead_time_override_changes_plan_and_is_logged(self, _today):
        self.client.force_login(self.planner)
        response = self.client.post(reverse('mrp:lead_times'),
                                    {'sku': 'COMP-5010', 'lead_time_days': 120, 'note': 'Supplier delays'})
        self.assertEqual(response.status_code, 302)
        result = services.build_plan(services.default_options())
        bolt = next(p for p in result.all_plans if p.sku == 'COMP-5010')
        self.assertEqual(bolt.component.lead_time_days, 120)
        self.assertEqual(result.inputs.lead_time_sources['COMP-5010'], 'override')
        log = models.SettingsChangeLog.objects.get()
        self.assertEqual((log.setting, log.old_value, log.new_value), ('lead_time_override:COMP-5010', '', '120'))

        self.client.post(reverse('mrp:delete_lead_time', args=['COMP-5010']))
        self.assertFalse(models.LeadTimeOverride.objects.exists())
        self.assertEqual(models.SettingsChangeLog.objects.count(), 2)

    def test_lead_time_override_unknown_sku(self, _today):
        self.client.force_login(self.planner)
        response = self.client.post(reverse('mrp:lead_times'), {'sku': 'FG-100', 'lead_time_days': 10})
        self.assertContains(response, 'Unknown component SKU')

    def test_refresh_without_acumatica_config(self, _today):
        self.client.force_login(self.planner)
        response = self.client.post(reverse('mrp:refresh'), follow=True)
        self.assertContains(response, 'not connected yet')

    def test_unsafe_next_redirect_ignored(self, _today):
        self.client.force_login(self.planner)
        response = self.client.post(reverse('mrp:save_reminder_days'),
                                    {'reminder_days': 14, 'next': 'https://evil.example.com/'})
        self.assertEqual(response['Location'], reverse('mrp:plan'))

    def test_digest_toggle(self, _today):
        self.client.force_login(self.viewer)
        self.client.post(reverse('mrp:toggle_digest'))
        self.assertFalse(models.WeeklyDigestSubscription.objects.exists())   # viewer has no email
        self.viewer.email = 'vic@example.com'
        self.viewer.save()
        self.client.post(reverse('mrp:toggle_digest'))
        self.assertTrue(models.WeeklyDigestSubscription.objects.filter(user=self.viewer).exists())
        self.client.post(reverse('mrp:toggle_digest'))
        self.assertFalse(models.WeeklyDigestSubscription.objects.exists())


@mock.patch('mrp.services.today', return_value=ANCHOR)
class ExportTests(TestCase):
    def setUp(self):
        seed()
        self.client.force_login(make_user('vic', 'viewer'))

    def test_csv_grouped_by_vendor(self, _today):
        response = self.client.get(reverse('mrp:export_csv'))
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        data = [r for r in rows[2:] if r and r[2] != 'SUBTOTAL']
        vendors = [r[0] for r in data]
        self.assertEqual(vendors, sorted(vendors))
        bearing = next(r for r in data if r[2] == 'COMP-2250')
        self.assertEqual((bearing[4], bearing[13], bearing[16], bearing[18]), ('Stockout risk', 'Now', '500', '2100'))
        subtotals = {r[0]: r[18] for r in rows if len(r) > 2 and r[2] == 'SUBTOTAL'}
        self.assertEqual(subtotals['Pacific Drives'], '5950')
        self.assertEqual(sum(D(v) for v in subtotals.values()), D('10195'))

    def test_csv_formula_injection_escaped(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(description='=HYPERLINK("x")')
        content = self.client.get(reverse('mrp:export_csv')).content.decode()
        self.assertIn("'=HYPERLINK", content)

    def test_calendar(self, _today):
        response = self.client.get(reverse('mrp:calendar_ics'))
        body = response.content.decode()
        self.assertEqual(body.count('BEGIN:VEVENT'), 4)
        self.assertIn('DTSTART;VALUE=DATE:20261103', body)
        self.assertIn('TRIGGER:-P30D', body)
        self.assertIn('Order from Pacific Drives (overdue)', body)


class LoaderTests(TestCase):
    def test_seed_shift_keeps_numbers(self):
        data = json.loads(SAMPLE_FILE.read_text())
        shifted = shift_sample(data, dt.date(2027, 2, 15))
        months = sorted({r['month'] for r in shifted['demand'] if r['source'] == 'trailing_shipments'})
        self.assertEqual(months[0], '2026-08-01')
        loaders.load_datasets(shifted, models.SyncRun.SAMPLE)
        with mock.patch('mrp.services.today', return_value=dt.date(2027, 2, 15)):
            result = services.build_plan(services.default_options())
        self.assertEqual(result.summary.components_to_order, 6)
        self.assertEqual(result.summary.estimated_value, D('10195'))

    def test_failed_load_keeps_previous_data(self):
        seed()
        with self.assertRaises(loaders.DataLoadError):
            loaders.load_datasets({'vendors': [{'vendor_id': 'X', 'name': 'X'}],
                                   'on_hand': [{'sku': 'A', 'warehouse': 'M', 'qty': 'lots'}]}, models.SyncRun.IMPORT)
        self.assertEqual(models.Vendor.objects.count(), 5)
        self.assertEqual(models.SyncRun.objects.first().status, models.SyncRun.FAILED)

    def test_demand_load_replaces_only_its_source(self):
        seed()
        loaders.load_datasets({'demand': [{'sku': 'FG-100', 'source': 'forecast', 'month': '2026-11-15', 'qty': 5},
                                          {'sku': 'FG-100', 'source': 'forecast', 'month': '2026-11-02', 'qty': 7}]},
                              models.SyncRun.IMPORT)
        self.assertEqual(models.DemandBucket.objects.get(source='forecast').qty, 12)
        self.assertTrue(models.DemandBucket.objects.filter(source='trailing_shipments').exists())

    def test_import_csv_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'vendors.csv').write_text('vendor_id,name,lead_time_days,email\nV1,Acme,21,\n')
            Path(tmp, 'stock_items.csv').write_text('sku,description,item_class,kind,uom,lot_size\nC1,Bolt,,component,EA,\n')
            call_command('import_data', csv_dir=tmp, stdout=io.StringIO())
        self.assertEqual(models.Vendor.objects.get().lead_time_days, 21)
        self.assertIsNone(models.StockItem.objects.get().lot_size)

    def test_import_rejects_bad_kind(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump({'stock_items': [{'sku': 'C1', 'kind': 'gadget'}]}, fh)
        with self.assertRaisesRegex(CommandError, 'kind must be'):
            call_command('import_data', json_path=fh.name, stdout=io.StringIO())


@override_settings(ACUMATICA={'BASE_URL': 'https://erp.example.com', 'TENANT': 'T', 'USERNAME': 'u', 'PASSWORD': 'p',
                              'TIMEOUT': 5, 'GI_NAMES': {
                                  'stock_items': 'S', 'on_hand': 'O', 'open_po_lines': 'P', 'bom': 'B', 'vendors': 'V',
                                  'item_vendors': 'IV', 'po_history': 'H', 'demand_shipments': 'DS',
                                  'demand_sales_orders': 'DSO', 'demand_forecast': 'DF'}})
class SyncTests(TestCase):
    GI_ROWS = {
        'V': [{'VendorID': 'V1', 'VendorName': 'Acme', 'LeadTimeDays': 30, 'Email': ''}],
        'S': [{'InventoryID': 'C1', 'Description': 'Bolt', 'ItemClass': '', 'IsFinishedGood': False, 'BaseUnit': 'EA', 'LotSize': 10},
              {'InventoryID': 'F1', 'Description': 'Pump', 'ItemClass': '', 'IsFinishedGood': True, 'BaseUnit': 'EA', 'LotSize': None}],
        'O': [{'InventoryID': 'C1', 'Warehouse': 'MAIN', 'QtyOnHand': 5}],
        'P': [],
        'B': [{'FinishedGood': 'F1', 'Component': 'C1', 'QtyPerUnit': 2}],
        'IV': [{'InventoryID': 'C1', 'VendorID': 'V1', 'IsDefault': True, 'LeadTimeDays': None, 'LastCost': 1.5, 'LotSize': None}],
        'H': [],
        'DS': [{'InventoryID': 'F1', 'ShipmentDate': '2026-09-03T00:00:00', 'ShippedQty': 30},
               {'InventoryID': 'F1', 'ShipmentDate': '2026-09-20T00:00:00', 'ShippedQty': 30}],
    }

    def fake_client(self, fail_on=None):
        client = mock.Mock()

        def fetch(gi, select=None, filter=''):
            if gi == fail_on:
                raise AcumaticaRequestError('boom', 500)
            return self.GI_ROWS[gi]
        client.fetch_rows.side_effect = fetch
        return client

    def test_sync_loads_everything(self):
        run = sync.run_sync(client=self.fake_client())
        self.assertEqual(run.status, models.SyncRun.OK)
        self.assertEqual(models.StockItem.objects.get(sku='F1').kind, 'finished_good')
        self.assertEqual(models.DemandBucket.objects.get().qty, 60)
        with mock.patch('mrp.services.today', return_value=dt.date(2026, 10, 5)):
            result = services.build_plan(services.default_options())
        bolt = result.all_plans[0]
        self.assertEqual(bolt.usage_per_month, 20)        # 60 shipped / 6 months x 2 per unit
        self.assertEqual(bolt.component.lead_time_days, 30)

    def test_sync_failure_keeps_data(self):
        seed()
        with self.assertRaises(AcumaticaRequestError):
            sync.run_sync(client=self.fake_client(fail_on='B'))
        self.assertEqual(models.Vendor.objects.count(), 5)
        run = models.SyncRun.objects.filter(source=models.SyncRun.ACUMATICA).get()
        self.assertEqual(run.status, models.SyncRun.FAILED)
        self.assertIsNotNone(run.finished_at)

    def test_concurrent_sync_blocked(self):
        models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA)
        with self.assertRaises(sync.SyncInProgress):
            sync.run_sync(client=self.fake_client())

    def test_command_without_config(self):
        with override_settings(ACUMATICA={'BASE_URL': '', 'TENANT': '', 'USERNAME': '', 'PASSWORD': ''}):
            with self.assertRaisesRegex(CommandError, 'not configured'):
                call_command('sync_acumatica')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   MRP_REMINDER_TO=['purchasing@example.com'], DEFAULT_FROM_EMAIL='mrp@example.com')
@mock.patch('mrp.services.today', return_value=ANCHOR)
class ReminderCommandTests(TestCase):
    def setUp(self):
        seed()

    def test_sends_once_per_vendor_in_window(self, _today):
        call_command('send_order_reminders', stdout=io.StringIO())
        subjects = sorted(m.subject for m in mail.outbox)
        self.assertEqual(len(subjects), 3)   # Pacific, Cascade (order now), Northwest (reminder active)
        self.assertIn('Order reminder: Northwest Metals — order by Nov 3', subjects)
        self.assertEqual(mail.outbox[0].to, ['purchasing@example.com'])
        # Second run the same day sends nothing new
        call_command('send_order_reminders', stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 3)

    def test_wider_window_includes_more_vendors(self, _today):
        settings_obj = models.PlanSettings.load()
        settings_obj.reminder_days = 60
        settings_obj.save()
        call_command('send_order_reminders', stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 4)

    def test_dry_run_sends_nothing(self, _today):
        out = io.StringIO()
        call_command('send_order_reminders', dry_run=True, stdout=out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(models.ReminderSent.objects.exists())
        self.assertIn('Pacific Drives', out.getvalue())

    def test_weekly_digest(self, _today):
        user = make_user('pat', 'planner', 'pat@example.com')
        models.WeeklyDigestSubscription.objects.create(user=user)
        with override_settings(MRP_REMINDER_TO=[]):
            call_command('send_order_reminders', weekly=True, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['pat@example.com'])
        self.assertIn('6 of 11 components to order', mail.outbox[0].body)

    def test_no_data(self, _today):
        models.SyncRun.objects.all().delete()
        with self.assertRaisesRegex(CommandError, 'No planning data'):
            call_command('send_order_reminders')
