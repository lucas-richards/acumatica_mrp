import copy
import datetime as dt
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planning.demand import add_months, month_start

from mrp import loaders, models
from mrp.services import today

SAMPLE_FILE = settings.BASE_DIR / 'sample_data' / 'sample_data.json'

DATE_FIELDS = {'open_po_lines': 'promised_date', 'po_history': 'order_date'}


def shift_sample(data: dict, to_date: dt.date) -> dict:
    """Move every date in the sample so it is relative to `to_date` instead of the anchor date."""
    data = copy.deepcopy(data)
    anchor = dt.date.fromisoformat(data['anchor_date'])
    day_shift = to_date - anchor
    month_shift = (to_date.year - anchor.year) * 12 + (to_date.month - anchor.month)
    for dataset, field in DATE_FIELDS.items():
        for row in data.get(dataset, []):
            if row.get(field):
                row[field] = (dt.date.fromisoformat(row[field]) + day_shift).isoformat()
    for row in data.get('demand', []):
        row['month'] = add_months(month_start(dt.date.fromisoformat(row['month'])), month_shift).isoformat()
    return data


class Command(BaseCommand):
    help = 'Load realistic sample data (5 vendors, 11 components, 5 finished goods) so the app runs without Acumatica.'

    def add_arguments(self, parser):
        parser.add_argument('--as-of', help='Make the sample relative to this date (YYYY-MM-DD). Default: today.')
        parser.add_argument('--no-shift', action='store_true', help='Keep the original sample dates.')

    def handle(self, *args, **options):
        data = json.loads(SAMPLE_FILE.read_text())
        if not options['no_shift']:
            as_of = dt.date.fromisoformat(options['as_of']) if options['as_of'] else today()
            data = shift_sample(data, as_of)
        try:
            run = loaders.load_datasets(data, models.SyncRun.SAMPLE)
        except loaders.DataLoadError as exc:
            raise CommandError(str(exc))
        counts = ', '.join(f'{k}: {v}' for k, v in run.counts.items())
        self.stdout.write(self.style.SUCCESS(f'Sample data loaded ({counts}).'))
