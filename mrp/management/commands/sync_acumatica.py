from django.core.management.base import BaseCommand, CommandError

from acumatica.odata import AcumaticaError
from planning.demand import DEMAND_SOURCES

from mrp import loaders, sync


class Command(BaseCommand):
    help = 'Refresh local planning data from Acumatica Generic Inquiries (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--demand-source', action='append', choices=list(DEMAND_SOURCES),
                            help='Demand source(s) to pull. Default: the source chosen in plan settings.')
        parser.add_argument('--all-demand', action='store_true', help='Pull all three demand sources.')

    def handle(self, *args, **options):
        if not sync.acumatica_configured():
            raise CommandError('Acumatica is not configured. Set ACUMATICA_BASE_URL, ACUMATICA_TENANT, '
                               'ACUMATICA_USERNAME and ACUMATICA_PASSWORD (see .env.example), '
                               'or use seed_demo / import_data instead.')
        sources = list(DEMAND_SOURCES) if options['all_demand'] else options['demand_source']
        try:
            run = sync.run_sync(demand_sources=sources)
        except (AcumaticaError, loaders.DataLoadError, sync.SyncInProgress) as exc:
            raise CommandError(f'Sync failed, existing data kept: {exc}')
        counts = ', '.join(f'{k}: {v}' for k, v in run.counts.items())
        self.stdout.write(self.style.SUCCESS(f'Sync complete ({counts}).'))
