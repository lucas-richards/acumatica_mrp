import csv
import datetime as dt
import io
import re
import json
import os
import tempfile
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from acumatica.rest import AcumaticaPermissionError
from acumatica.tests.test_entities import FakeClient, base_records
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

    def test_plan_page(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertEqual(response.status_code, 200)
        for text in ['Sample data', 'Stockout risk', 'Months left when an order arrives',
                     'class="bar-label label-lead"', 'line-lead', 'line-reorder', 'runs out before it arrives',
                     'Showing quantity <strong>on hand</strong>']:
            self.assertContains(response, text)
        # Removed from the page: summary tiles, cover/quantity choices, reminders
        for text in ['Components to order', 'Cover after new order arrives', 'Order quantities',
                     'Order reminders by vendor', 'Remind me', 'Refresh from Acumatica', 'Create PO']:
            self.assertNotContains(response, text)

    def test_compact_header_and_labelled_lines(self, _today):
        self.client.force_login(make_user('pam', 'planner'))
        html = self.client.get(reverse('mrp:plan')).content.decode()
        # the explanation, title, legend, footer note and item count are gone
        for gone in ['Items to purchase</h2>', 'section-intro', 'Reorder point (lead time', 'class="legend"',
                     'id="filter-count"', 'Showing X', 'table-foot', 'Vendors with something to order</h2>']:
            self.assertNotIn(gone, html)
        # export sits in the header next to refresh, and is the small button
        header = html[html.index('<header class="page-header">'):html.index('</header>')]
        self.assertIn('btn-small', header)
        self.assertLess(header.index('Export CSV'), header.index('Refresh from Acumatica'))
        # each supply bar labels the red line LT and the black line with the days after it
        row = html[html.index('data-search="comp-2250'):]
        row = row[:row.index('</tr>')]
        self.assertIn('label-lead" style="left:', row)
        self.assertIn('>LT</span>', row)
        self.assertRegex(row, r'label-reorder"[^>]*>\+\d+d</span>')
        # the number carries the rounded highlight; the cell is no longer painted
        self.assertIn('class="usage-num"', row)

    def test_months_at_arrival_column(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        bearing = next(p for p in response.context['result'].plans if p.sku == 'COMP-2250')
        self.assertEqual(round(bearing.months_at_arrival, 1), D('-3.3'))
        self.assertContains(response, '-3.3 mo')

    def test_available_and_on_order_since(self, _today):
        models.StockItem.objects.update(qty_available=7)
        models.OpenPOLine.objects.filter(sku='COMP-2250').update(order_date=dt.date(2026, 8, 3))
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertContains(response, '>Available</th>')
        self.assertNotContains(response, 'Showing quantity <strong>on hand</strong>')
        self.assertContains(response, 'since Aug 3, 2026')
        bearing = next(p for p in response.context['result'].plans if p.sku == 'COMP-2250')
        self.assertEqual(bearing.component.on_hand, 7)

    def test_vendor_chips_show_how_many_items_to_order(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        chips = [(name, count) for _, name, count, _ in response.context['vendor_chips']]
        # sample data: 5 items to order across 3 vendors; the other two vendors have nothing to order
        self.assertEqual(chips[0], ('All vendors', None))
        self.assertEqual(sorted(chips[1:], key=lambda c: c[0]),
                         [('Cascade Cable Co', 1), ('Northwest Metals', 2), ('Pacific Drives', 2)])
        self.assertGreaterEqual(chips[1][1], chips[2][1])          # most items first
        html = response.content.decode()
        self.assertIn('class="chip-count"', html)
        self.assertIn('2 items to order', html)
        self.assertNotIn('Summit Seals</a>', html)
        # the vendor is under the item, not a column of its own
        self.assertIn('class="vendor-name">Pacific Drives', html)
        self.assertNotIn('data-filter="vendor"', html)
        self.assertNotIn('class="vendor-cell"', html)
        self.assertIn('data-filter="item"', html)
        self.assertIn('data-filter="status"', html)

    def test_picked_vendor_stays_in_the_chips(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), {'vendor': 'SUMSEA'})
        chips = {name: count for _, name, count, _ in response.context['vendor_chips']}
        self.assertEqual(chips['Summit Seals'], 0)
        self.assertEqual({p.component.vendor_id for p in response.context['result'].plans}, {'SUMSEA'})

    def test_manufactured_components_not_on_purchasing(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(replenishment_source='Manufacturing')
        models.StockItem.objects.filter(sku='COMP-2310').update(replenishment_source='Mixed')
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        skus = {p.sku for p in response.context['result'].plans}
        self.assertNotIn('COMP-2250', skus)
        self.assertEqual(len(skus), 10)
        self.assertContains(response, 'Purchase or manufacture')

    def test_items_with_planning_method_none_are_not_listed(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(planning_method='None')
        models.StockItem.objects.filter(sku='COMP-2310').update(planning_method='MRP')
        models.StockItem.objects.filter(sku='COMP-3105').update(planning_method='Inventory Replenishment')
        # items the inquiry did not describe (blank) stay listed
        self.client.force_login(self.viewer)
        skus = {p.sku for p in self.client.get(reverse('mrp:plan')).context['result'].plans}
        self.assertNotIn('COMP-2250', skus)
        self.assertTrue({'COMP-2310', 'COMP-3105', 'COMP-3120'} <= skus)
        self.assertEqual(len(skus), 10)

    def test_kit_assembly_items_are_not_purchased(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(replenishment_source='Kit Assembly')
        self.client.force_login(self.viewer)
        skus = {p.sku for p in self.client.get(reverse('mrp:plan')).context['result'].plans}
        self.assertNotIn('COMP-2250', skus)

    def test_compounding_skus_not_on_purchasing(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(sku='XLDXRX-2250')
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        skus = {p.sku for p in response.context['result'].plans}
        self.assertNotIn('XLDXRX-2250', skus)
        self.assertEqual(len(skus), 10)

    def test_breakdown_lists_affected_finished_goods(self, _today):
        self.client.force_login(self.viewer)
        detail = self.client.get(reverse('mrp:breakdown', args=['COMP-2250']))
        for text in ['FG-100', 'FG-400', 'Months of stock', 'Last production run', 'Sep 14, 2026', 'Component for a run']:
            self.assertContains(detail, text)
        plan = next(p for p in self.client.get(reverse('mrp:plan')).context['result'].plans if p.sku == 'COMP-2250')
        rows = {r.fg_sku: r for r in plan.breakdown}
        self.assertEqual(rows['FG-100'].last_run_qty, 60)
        self.assertEqual(rows['FG-100'].last_run_component_qty, 120)   # 60 x 2 per unit
        self.assertEqual(rows['FG-100'].fg_demand_per_month, 40)

    def test_no_finished_goods_toggle(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertContains(response, 'Items to purchase')
        self.assertContains(response, 'Impact')
        self.assertNotContains(response, 'view-chips')

    def test_plan_is_reused_until_new_data_or_settings(self, _today):
        self.client.force_login(self.viewer)
        with mock.patch('mrp.services.load_inputs', wraps=services.load_inputs) as load:
            self.client.get(reverse('mrp:plan'))
            self.client.get(reverse('mrp:plan'), {'vendor': 'NWMETL'})      # switching vendor
            self.client.get(reverse('mrp:export_csv'))
            self.client.get(reverse('mrp:plan'))                            # coming back
            self.assertEqual(load.call_count, 1)
            # new data (a sync, an import, the seed) rebuilds it once
            loaders.load_datasets(json.loads(SAMPLE_FILE.read_text()), models.SyncRun.SAMPLE)
            self.client.get(reverse('mrp:plan'))
            self.client.get(reverse('mrp:plan'), {'vendor': 'PACDRV'})
            self.assertEqual(load.call_count, 2)
            # a different usage chosen for an item is planned again without rebuilding everything
            planner = make_user('pam', 'planner')
            self.client.force_login(planner)
            self.client.post(reverse('mrp:set_usage'), {'sku': 'COMP-2250', 'basis': 'two_year'})
            self.client.get(reverse('mrp:plan'))
            self.assertEqual(load.call_count, 2)

    def test_in_receiving_shown_in_on_order(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(qty_receiving=D('120'))
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertContains(response, 'In Receiving</th>')
        self.assertContains(response, '<span class="receiving">120</span>')
        bearing = next(p for p in response.context['result'].plans if p.sku == 'COMP-2250')
        self.assertEqual(bearing.component.in_receiving, 120)
        self.assertEqual(bearing.supply_position, 145)      # 80 on hand and on order + 120 - 55 on open orders

    def test_header_explanations_and_sticky_header(self, _today):
        self.client.force_login(self.viewer)
        html = self.client.get(reverse('mrp:plan')).content.decode()
        self.assertEqual(html.count('data-tip='), 8)        # the header cells only, not each row
        for text in ['Usage per month', 'Supply ÷ usage per month',
                     'Suggested = usage × reorder point − supply', 'Months left with this order', 'Monthly usage, 2 years']:
            self.assertIn(text, html)
        self.assertIn('plan-scroll', html)

    def test_two_year_average_column(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertContains(response, 'Mo usage (2 year)')
        self.assertContains(response, 'Mo usage (6 months)')
        html = response.content.decode()
        self.assertLess(html.index('Mo usage (2 year)'), html.index('Mo usage (6 months)'))
        self.assertNotContains(response, 'Demand in lead time</th>')
        self.assertNotContains(response, 'On open orders</th>')
        bearing = next(p for p in response.context['result'].plans if p.sku == 'COMP-2250')
        # sample issues for the seven months before the anchor, averaged over 24 months
        self.assertGreater(bearing.component.active_months, 0)
        self.assertLessEqual(bearing.component.active_months, 24)
        self.assertNotContains(response, ' of 24 months')
        self.assertEqual(response.context['result'].plans[0].component.long_term_usage is not None, True)

    def test_negative_available_shows_back_ordered(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(qty_available=D('-40'))
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        self.assertContains(response, 'Back ordered')
        self.assertEqual(response.content.decode().count('Back ordered</span>'), 1)
        self.assertNotContains(response, '-40 available')

    def test_order_box_and_months_left_with_order(self, _today):
        self.client.force_login(make_user('pam', 'planner'))
        response = self.client.get(reverse('mrp:plan'))
        html = response.content.decode()
        self.assertContains(response, 'name="qty"')
        self.assertNotContains(response, 'type="number"')
        self.assertContains(response, 'value="0"')        # the order box starts at 0 (COMP-2250's last order was 600)
        self.assertNotContains(response, 'value="600"')
        self.assertContains(response, 'Months left with this order')
        self.assertContains(response, 'js-with-order')
        self.assertContains(response, 'data-usage="110.000000"')
        self.assertContains(response, 'data-stock="80.000000"')      # 50 available + 30 on order
        for gone in ['Add to PO', 'Purchase requests', 'Lead times']:
            self.assertNotContains(response, gone)
        # a viewer sees the quantity but no box and no way to change usage
        self.client.force_login(self.viewer)
        viewer_page = self.client.get(reverse('mrp:plan'))
        self.assertNotContains(viewer_page, 'name="qty"')
        self.assertNotContains(viewer_page, 'name="custom"')
        self.assertNotContains(viewer_page, 'hx-post')

    def test_months_left_says_what_is_missing(self, _today):
        models.ItemVendor.objects.filter(sku='COMP-2250').update(lead_time_days=None)
        models.Vendor.objects.filter(vendor_id='PACDRV').update(lead_time_days=None)
        models.UsageChoice.objects.create(sku='COMP-3105', basis='custom', custom_usage=0)
        models.UsageChoice.objects.create(sku='COMP-2250', basis='custom', custom_usage=0)      # no lead time and no usage
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'))
        html = response.content.decode()

        def row(sku):
            part = html[html.index('data-sku="%s"' % sku):]
            return part[:part.index('</tr>')]

        lead = row('COMP-2250')
        self.assertEqual(lead.count('Need lead time and demand</span>'), 2)    # both months-left cells
        self.assertIn('data-need="lead time and demand"', lead)
        self.assertIn('Need demand</span>', row('COMP-3105'))
        self.assertIn('data-need="demand"', row('COMP-3105'))
        self.assertIn('Need lead time</span>', row('COMP-2310'))        # same vendor: no lead time, but it has usage
        self.assertNotIn('Need ', row('COMP-4408'))
        csv_text = self.client.get(reverse('mrp:export_csv')).content.decode()
        self.assertIn('need lead time', csv_text)
        self.assertIn('need demand', csv_text)

    def test_purchase_requisition_sheet(self, _today):
        self.client.force_login(self.viewer)
        html = self.client.get(reverse('mrp:plan')).content.decode()
        plans = self.client.get(reverse('mrp:plan')).context['result'].plans
        self.assertEqual(html.count('data-report-url='), len(plans))
        response = self.client.get(reverse('mrp:report', args=['COMP-2250']) + '?qty=1,500')
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        for expected in ('Purchase requisition', 'Requested by', 'Quantity to order', 'Needed by', 'Monthly usage',
                         '2 year average', '2 year graph inventory levels', 'value="1500"', 'type="date"'):
            self.assertIn(expected, text)
        self.assertIn(self.viewer.get_username(), text)
        row = next(x for x in plans if x.sku == 'COMP-2250')
        if not row.missing_for_months_left:
            from mrp.templatetags.mrp_format import months
            self.assertIn(f'{months(row.months_at_arrival_with(D(1500)))} mo', text)
        self.assertEqual(self.client.get(reverse('mrp:report', args=['NOPE'])).status_code, 404)
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse('mrp:report', args=['COMP-2250'])).status_code, 200)

    def test_graph_icon_opens_a_panel_under_the_item(self, _today):
        self.client.force_login(self.viewer)                      # a read-only feature: viewers get it too
        response = self.client.get(reverse('mrp:plan'))
        html = response.content.decode()
        plans = response.context['result'].plans
        self.assertEqual(html.count('class="chart-btn"'), len(plans))
        self.assertEqual(html.count('data-history-pane'), len(plans))
        self.assertNotIn('<dialog', html)                        # no pop-up any more
        self.assertNotIn('history-dialog', html)
        # the icon opens the same detail row as the finished-goods button
        sku_id = 'COMP-2250'.encode().hex()
        row = html[html.index('data-sku="COMP-2250"'):]
        row = row[:row.index('</tr>')]
        self.assertIn('class="chart-btn" data-toggle-row aria-expanded="false" aria-controls="bd-%s"' % sku_id, row)
        self.assertIn('class="expand-btn" data-toggle-row aria-expanded="false" aria-controls="bd-%s"' % sku_id, row)
        # the detail row: finished goods on the left (loaded when opened), the chart pane on the right
        detail = html[html.index('<tr id="bd-%s"' % sku_id):]
        detail = detail[:detail.index('</tr>')]
        self.assertIn('class="detail-fg"', detail)
        self.assertIn('hx-trigger="load-detail once"', detail)
        self.assertLess(detail.index('detail-fg'), detail.index('detail-chart'))
        self.assertIn('<h3 class="detail-title">2 year graph inventory levels</h3>', detail)
        self.assertIn('data-sku="COMP-2250"', detail)
        # the data and library locations are on the table, and the library is never loaded with the page
        self.assertIn('data-history-url="/planner/json/"', html)
        self.assertRegex(html, r'data-chart-js="[^"]*chart\.umd\.min\.js\?v=\d+"')
        self.assertNotIn('<script src="/static/mrp/chart', html)
        # an item that is in no BOM gets the chart pane alone
        models.StockItem.objects.create(sku='COMP-LONE', description='In no BOM', kind='component')
        models.ItemVendor.objects.create(sku='COMP-LONE', vendor_id='PACDRV', is_default=True, lead_time_days=30)
        models.DemandBucket.objects.create(sku='COMP-LONE', source='trailing_issues', month=dt.date(2026, 9, 1), qty=60)
        services.clear_cache()
        html = self.client.get(reverse('mrp:plan')).content.decode()
        plain = html[html.index('<tr id="bd-%s"' % 'COMP-LONE'.encode().hex()):]
        plain = plain[:plain.index('</tr>')]
        self.assertNotIn('detail-fg', plain)
        self.assertIn('2 year graph inventory levels', plain)
        # a swapped-in row (changing a usage) keeps its icon
        self.client.force_login(make_user('pam', 'planner'))
        swapped = self.client.post(reverse('mrp:set_usage'), {'sku': 'COMP-2250', 'basis': 'two_year'})
        self.assertContains(swapped, 'class="chart-btn" data-toggle-row')

    @override_settings(INVENTORY_HISTORY_URL='http://10.1.1.19:8000/planner/json/')
    def test_history_url_is_a_setting(self, _today):
        self.client.force_login(self.viewer)
        self.assertContains(self.client.get(reverse('mrp:plan')), 'data-history-url="http://10.1.1.19:8000/planner/json/"')

    def test_chart_library_is_vendored(self, _today):
        path = Path(settings.BASE_DIR) / 'static' / 'mrp' / 'chart.umd.min.js'
        self.assertTrue(path.exists())
        self.assertIn('Chart.js v4.4.1', path.read_text()[:200])

    def _flags(self, sku):
        html = self.client.get(reverse('mrp:plan')).content.decode()
        row = html[html.index('data-sku="%s"' % sku):]
        return row[:row.index('</tr>')]

    def test_last_po_vendor_other_than_default_is_warned(self, _today):
        models.PoHistory.objects.filter(sku='COMP-2250').update(vendor_id='CASCAB')       # last PO went elsewhere
        self.client.force_login(make_user('pam', 'planner'))
        row = self._flags('COMP-2250')
        self.assertIn('flag-warn', row)
        self.assertIn('Last PO: Cascade Cable Co', row)
        self.assertIn('default: Pacific Drives', row)
        self.assertIn('class="flag-done"', row)
        # a viewer sees the warning but has no Done button
        self.client.force_login(self.viewer)
        viewer_row = self._flags('COMP-2250')
        self.assertIn('flag-warn', viewer_row)
        self.assertNotIn('flag-done', viewer_row)
        # items whose last PO matches the default have none
        self.assertNotIn('flag-warn', self._flags('COMP-2310'))

    def test_no_default_vendor_is_warned(self, _today):
        models.ItemVendor.objects.filter(sku='COMP-3105').update(is_default=False)
        self.client.force_login(make_user('pam', 'planner'))
        self.assertIn('No default vendor set', self._flags('COMP-3105'))

    def test_done_hides_a_vendor_warning_for_a_week_then_it_returns(self, _today):
        models.ItemVendor.objects.filter(sku='COMP-3105').update(is_default=False)
        self.client.force_login(make_user('pam', 'planner'))
        response = self.client.post(reverse('mrp:warning_done'), {'sku': 'COMP-3105', 'flag': 'no_default_vendor'},
                                    HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'COMP-3105')
        self.assertNotContains(response, 'No default vendor set')              # the returned row has no warning
        self.assertNotIn('No default vendor set', self._flags('COMP-3105'))
        # the CSV does not list it either
        self.assertNotIn('No default vendor', self.client.get(reverse('mrp:export_csv')).content.decode())
        # six days later it is still hidden; on the seventh it is back, as long as it is not fixed
        for days, shown in ((6, False), (7, True)):
            with mock.patch('mrp.services.today', return_value=ANCHOR + dt.timedelta(days=days)):
                services.clear_cache()
                self.assertEqual('No default vendor set' in self._flags('COMP-3105'), shown, days)
        # fixed meanwhile: nothing to show
        models.ItemVendor.objects.filter(sku='COMP-3105').update(is_default=True)
        with mock.patch('mrp.services.today', return_value=ANCHOR + dt.timedelta(days=8)):
            services.clear_cache()
            self.assertNotIn('No default vendor set', self._flags('COMP-3105'))

    def test_done_only_for_vendor_warnings_and_planners(self, _today):
        url = reverse('mrp:warning_done')
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(url, {'sku': 'COMP-3105', 'flag': 'no_default_vendor'}).status_code, 403)
        self.client.force_login(make_user('pam', 'planner'))
        self.assertEqual(self.client.post(url, {'sku': 'COMP-3105', 'flag': 'po_late'}).status_code, 400)
        self.assertEqual(self.client.post(url, {'sku': 'NOPE', 'flag': 'vendor_mismatch'}).status_code, 404)
        self.assertFalse(models.WarningAck.objects.exists())

    def test_removed_screens_are_gone(self, _today):
        self.client.force_login(make_user('pam', 'planner'))
        for path in ('requests/', 'lead-times/', 'add-to-po/'):
            self.assertEqual(self.client.get('/' + path).status_code, 404)

    def test_the_usage_in_use_is_highlighted(self, _today):
        self.client.force_login(self.viewer)
        html = self.client.get(reverse('mrp:plan')).content.decode()
        self.assertIn('Mo usage (custom)', html)
        row = html[html.index('data-search="comp-2250'):]
        row = row[:row.index('</tr>')]
        self.assertEqual(row.count('is-used'), 1)               # exactly one usage is in use
        self.assertIn('in use', row)

    def test_choosing_which_usage_drives_the_plan(self, _today):
        self.client.force_login(make_user('pam', 'planner'))
        url = reverse('mrp:set_usage')

        def bearing():
            plans = services.build_plan(services.default_options()).all_plans
            return next(p for p in plans if p.sku == 'COMP-2250')

        recent = bearing()
        self.assertEqual((recent.usage_basis, recent.usage_per_month), ('six', 110))
        # use the two year average instead: every figure follows
        response = self.client.post(url, {'sku': 'COMP-2250', 'basis': 'two_year'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'COMP-2250')
        two_year = bearing()
        self.assertEqual(two_year.usage_basis, 'two_year')
        self.assertEqual(two_year.usage_per_month, two_year.component.long_term_usage)
        self.assertNotEqual(two_year.months_of_supply, recent.months_of_supply)
        self.assertContains(response, 'data-usage="%s"' % ('%.6f' % two_year.usage_per_month))
        # typing a custom usage selects it and drives the plan
        response = self.client.post(url, {'sku': 'COMP-2250', 'custom': '1,500'}, HTTP_HX_REQUEST='true')
        custom = bearing()
        self.assertEqual((custom.usage_basis, custom.usage_per_month), ('custom', 1500))
        self.assertGreater(custom.suggested_qty, recent.suggested_qty)
        self.assertContains(response, 'value="1500"')
        # click another usage to go back
        self.client.post(url, {'sku': 'COMP-2250', 'basis': 'six'})
        self.assertEqual(bearing().usage_basis, 'six')
        self.assertEqual(models.UsageChoice.objects.get(sku='COMP-2250').custom_usage, 1500)   # kept for later
        # selecting custom again, then clearing the value falls back to the recent usage
        self.client.post(url, {'sku': 'COMP-2250', 'basis': 'custom'})
        self.assertEqual(bearing().usage_per_month, 1500)
        self.client.post(url, {'sku': 'COMP-2250', 'custom': ''})
        cleared = bearing()
        self.assertEqual((cleared.usage_basis, cleared.usage_per_month), ('six', 110))

    def test_usage_choice_is_remembered_and_rebuilt_in_other_processes(self, _today):
        self.client.force_login(make_user('pam', 'planner'))
        self.client.post(reverse('mrp:set_usage'), {'sku': 'COMP-2250', 'custom': '300'})
        services.clear_cache()                                     # as if another process built it from scratch
        plan = next(p for p in services.build_plan(services.default_options()).all_plans if p.sku == 'COMP-2250')
        self.assertEqual((plan.usage_basis, plan.usage_per_month), ('custom', 300))

    def test_usage_choice_rejects_bad_input(self, _today):
        url = reverse('mrp:set_usage')
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(url, {'sku': 'COMP-2250', 'basis': 'two_year'}).status_code, 403)
        self.client.force_login(make_user('pam', 'planner'))
        self.assertEqual(self.client.post(url, {'sku': 'COMP-2250', 'basis': 'nonsense'}).status_code, 400)
        self.assertEqual(self.client.post(url, {'sku': 'NOPE', 'basis': 'six'}).status_code, 404)
        self.client.post(url, {'sku': 'COMP-2250', 'custom': 'abc'})
        self.client.post(url, {'sku': 'COMP-2250', 'custom': '-5'})
        plan = next(p for p in services.build_plan(services.default_options()).all_plans if p.sku == 'COMP-2250')
        self.assertEqual((plan.usage_basis, plan.usage_per_month), ('six', 110))

    def test_vendor_filter(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), {'vendor': 'NWMETL'})
        plans = response.context['result'].plans
        self.assertEqual({p.component.vendor_id for p in plans}, {'NWMETL'})
        self.assertEqual(response.context['result'].summary.components_to_order, 2)

    def test_quantity_mode_is_fixed(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), {'vendor': 'NOPE', 'mode': 'last_po'})
        options = response.context['options']
        self.assertEqual((options.vendor_id, options.qty_mode.value), (None, 'cover'))

    def test_htmx_returns_partial(self, _today):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('mrp:plan'), HTTP_HX_REQUEST='true')
        self.assertNotContains(response, '<html')
        self.assertContains(response, 'Months left when an order arrives')

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
        self.assertContains(response, 'Nothing to purchase')
        self.assertContains(response, 'No data loaded')


@mock.patch('mrp.services.today', return_value=ANCHOR)
class SettingsTests(TestCase):
    def setUp(self):
        seed()
        self.planner = make_user('pat', 'planner')
        self.viewer = make_user('vic', 'viewer')

    def test_viewer_cannot_change_settings(self, _today):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(reverse('mrp:refresh')).status_code, 403)

    def test_refresh_without_acumatica_config(self, _today):
        self.client.force_login(self.planner)
        response = self.client.post(reverse('mrp:refresh'), follow=True)
        self.assertContains(response, 'not connected yet')

    def test_unsafe_next_redirect_ignored(self, _today):
        self.client.force_login(self.planner)
        response = self.client.post(reverse('mrp:refresh'), {'next': 'https://evil.example.com/'})
        self.assertEqual(response['Location'], reverse('mrp:plan'))


@mock.patch('mrp.services.today', return_value=ANCHOR)
class ExportTests(TestCase):
    def setUp(self):
        seed()
        self.client.force_login(make_user('vic', 'viewer'))

    def test_csv_grouped_by_vendor(self, _today):
        response = self.client.get(reverse('mrp:export_csv'))
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertTrue(all(rows[2:]), 'no blank rows')
        data = [r for r in rows[2:] if r and r[2] != 'SUBTOTAL']
        vendors = [r[0] for r in data]
        self.assertEqual(vendors, sorted(vendors))
        bearing = next(r for r in data if r[2] == 'COMP-2250')
        self.assertEqual((bearing[4], bearing[11], bearing[12], bearing[17], bearing[18], bearing[19], bearing[21]),
                         ('Stockout risk', '50', '30', 'Now', '600', '600', '-3.3'))
        subtotals = {r[0]: r[3] for r in rows if len(r) > 2 and r[2] == 'SUBTOTAL'}
        self.assertEqual(sum(int(v.split()[0]) for v in subtotals.values()), 5)

    def test_csv_formula_injection_escaped(self, _today):
        models.StockItem.objects.filter(sku='COMP-2250').update(description='=HYPERLINK("x")')
        content = self.client.get(reverse('mrp:export_csv')).content.decode()
        self.assertIn("'=HYPERLINK", content)


class LoaderTests(TestCase):
    def test_seed_shift_keeps_numbers(self):
        data = json.loads(SAMPLE_FILE.read_text())
        shifted = shift_sample(data, dt.date(2027, 2, 15))
        months = sorted({r['month'] for r in shifted['demand'] if r['source'] == 'trailing_invoices'})
        self.assertEqual(months[0], '2026-08-01')
        loaders.load_datasets(shifted, models.SyncRun.SAMPLE)
        with mock.patch('mrp.services.today', return_value=dt.date(2027, 2, 15)):
            result = services.build_plan(services.default_options())
        self.assertEqual(result.summary.components_to_order, 5)

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
        self.assertTrue(models.DemandBucket.objects.filter(source='trailing_invoices').exists())

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


ACUMATICA_TEST_SETTINGS = {
    'BASE_URL': 'https://erp.example.com', 'TENANT': 'T', 'BRANCH': '', 'USERNAME': 'u', 'PASSWORD': 'p',
    'TIMEOUT': 5, 'ENDPOINT': 'Default/24.200.001', 'MANUFACTURING_ENDPOINT': 'MANUFACTURING/24.200.001',
    'SALES_ORDER_TYPES': ['SO'], 'AVAILABLE_GI': 'LR-Available Qty',
    'RECEIVING_GI': 'DB-StorageDetailsByItemWarehouseLocation', 'RECEIVING_LOCATION': 'REC-INS',
}


@override_settings(ACUMATICA=ACUMATICA_TEST_SETTINGS)
@mock.patch('mrp.services.today', return_value=dt.date(2026, 10, 30))
class SyncTests(TestCase):
    def test_sync_loads_everything(self, _today):
        run = sync.run_sync(client=FakeClient(base_records()))
        self.assertEqual(run.status, models.SyncRun.OK)
        self.assertTrue(any('CRATE' in w for w in run.warnings))
        self.assertEqual(models.StockItem.objects.get(sku='SA-1').kind, 'finished_good')
        self.assertEqual(models.DemandBucket.objects.get(sku='FG-1', source='trailing_invoices').month, dt.date(2026, 9, 1))
        result = services.build_plan(services.default_options())
        bolt = next(p for p in result.all_plans if p.sku == 'C-BOLT')
        # Component usage is issue history: 999 bolts issued in Sept over the 6-month window (a bulk sale is left out)
        self.assertEqual(bolt.usage_per_month, D(999) / 6)
        self.assertEqual(bolt.component.lead_time_days, 16)
        self.assertEqual(bolt.component.on_order, 200)
        self.assertEqual(bolt.committed, 0)   # FG-1's open sales orders already have production orders
        plate = next(p for p in result.all_plans if p.sku == 'C-PLATE')
        self.assertEqual(plate.component.lead_time_days, 30)   # vendor default
        # Plate usage = assembly out only (30 consumed - 5 returned) / 6 months
        self.assertEqual(plate.usage_per_month, D(25) / 6)
        # FG-1: invoices (8 - 2 credit) / 6, assembly out none; 4 on sales orders, all within the 12 to produce
        fg1 = next(f for f in result.inputs.finished_goods if f.sku == 'FG-1')
        self.assertEqual(fg1.demand_per_month, D(1))
        self.assertEqual(fg1.unproduced_orders, 0)
        # SA-1 is also a kit component: 3 consumed by an assembly + 1 disassembled over 6 months, no invoices
        sa1 = next(f for f in result.inputs.finished_goods if f.sku == 'SA-1')
        self.assertEqual(sa1.demand_per_month, D(4) / 6)

    def test_sync_failure_keeps_data(self, _today):
        seed()
        with self.assertRaises(AcumaticaPermissionError):
            sync.run_sync(client=FakeClient(base_records(), deny={'BillOfMaterial'}))
        self.assertEqual(models.Vendor.objects.count(), 5)
        run = models.SyncRun.objects.filter(source=models.SyncRun.ACUMATICA).get()
        self.assertEqual(run.status, models.SyncRun.FAILED)
        self.assertIn('AM208000', run.error)
        self.assertIsNotNone(run.finished_at)

    def test_dead_run_is_cleared(self, _today):
        models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA, pid=999999999)
        self.assertIsNone(sync.running_sync())
        run = models.SyncRun.objects.get()
        self.assertEqual(run.status, models.SyncRun.FAILED)
        self.assertIn('Interrupted', run.error)

    def test_refresh_starts_background_process(self, _today):
        planner = make_user('pat', 'planner')
        self.client.force_login(planner)
        with mock.patch('mrp.sync.subprocess.Popen') as popen:
            response = self.client.post(reverse('mrp:refresh'))
        self.assertEqual(response.status_code, 302)
        args = popen.call_args.args[0]
        self.assertEqual(args[-3:], ['sync_acumatica', '--user-id', str(planner.pk)])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_refresh_status_banner_then_reload(self, _today):
        self.client.force_login(make_user('vic', 'viewer'))
        run = models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA, pid=os.getpid())
        response = self.client.get(reverse('mrp:refresh_status'))
        self.assertContains(response, 'Refreshing from Acumatica')
        self.assertContains(response, 'hx-trigger="every 5s"')
        page = self.client.get(reverse('mrp:plan'))
        self.assertContains(page, 'Refreshing from Acumatica')
        run.status, run.counts = models.SyncRun.OK, {'bom': 3}
        run.save()
        response = self.client.get(reverse('mrp:refresh_status'))
        self.assertEqual(response['HX-Refresh'], 'true')

    def test_concurrent_sync_blocked(self, _today):
        models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA, pid=os.getpid())
        with self.assertRaises(sync.SyncInProgress):
            sync.run_sync(client=FakeClient(base_records()))

    def test_command_without_config(self, _today):
        with override_settings(ACUMATICA={**ACUMATICA_TEST_SETTINGS, 'USERNAME': '', 'PASSWORD': ''}):
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
        self.assertEqual(len(subjects), 3)   # Pacific, Cascade, Northwest (all order now)
        self.assertIn('Order reminder: Northwest Metals — order now', subjects)
        self.assertEqual(mail.outbox[0].to, ['purchasing@example.com'])
        # Second run the same day sends nothing new
        call_command('send_order_reminders', stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 3)

    def test_wider_window_includes_more_vendors(self, _today):
        settings_obj = models.PlanSettings.load()
        settings_obj.reminder_days = 60
        settings_obj.save()
        call_command('send_order_reminders', stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 3)   # nothing else is due, however wide the window

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
        self.assertIn('5 of 11 components to order', mail.outbox[0].body)

    def test_no_data(self, _today):
        models.SyncRun.objects.all().delete()
        with self.assertRaisesRegex(CommandError, 'No planning data'):
            call_command('send_order_reminders')
