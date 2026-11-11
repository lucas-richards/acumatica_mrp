from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from acumatica.rest import AcumaticaError
from planning.demand import DEMAND_SOURCES

from mrp import loaders, sync


class Command(BaseCommand):
    help = ('Refresh local planning data from Acumatica (read-only REST API): BOMs, stock items, vendors, '
            'purchase orders and finished-good demand.')

    def add_arguments(self, parser):
        parser.add_argument('--demand-source', action='append', choices=list(DEMAND_SOURCES),
                            help='Demand source(s) to pull. Default: the source chosen in plan settings.')
        parser.add_argument('--all-demand', action='store_true',
                            help='Pull trailing invoices and open sales orders.')
        parser.add_argument('--user-id', type=int, help='User who asked for the refresh (set by the web page).')

    def handle(self, *args, **options):
        if not sync.acumatica_configured():
            raise CommandError('Acumatica is not configured. Set ACUMATICA_BASE_URL, ACUMATICA_TENANT, '
                               'ACUMATICA_USERNAME and ACUMATICA_PASSWORD (see .env.example), '
                               'or use seed_demo / import_data instead.')
        if options['all_demand']:
            sources = ['trailing_invoices', 'open_sales_orders']
        else:
            sources = options['demand_source']
        try:
            user = get_user_model().objects.filter(pk=options['user_id']).first() if options['user_id'] else None
            run = sync.run_sync(user=user, demand_sources=sources)
        except (AcumaticaError, loaders.DataLoadError, sync.SyncInProgress) as exc:
            raise CommandError(f'Sync failed, existing data kept: {exc}')
        for warning in run.warnings:
            self.stderr.write(f'Warning: {warning}')
        counts = ', '.join(f'{k}: {v}' for k, v in run.counts.items())
        self.stdout.write(self.style.SUCCESS(f'Sync complete ({counts}); {len(run.warnings)} warning(s).'))
