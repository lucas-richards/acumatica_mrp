import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mrp import loaders, models


class Command(BaseCommand):
    help = ('Import planning data from a JSON file (same shape as sample_data/sample_data.json) '
            'or from CSV files named after each dataset. Each dataset present is fully replaced.')

    def add_arguments(self, parser):
        parser.add_argument('--json', dest='json_path', help='JSON file containing one or more datasets.')
        parser.add_argument('--csv-dir', help='Directory of CSV files, e.g. vendors.csv, stock_items.csv, '
                                              'on_hand.csv, open_po_lines.csv, bom.csv, item_vendors.csv, '
                                              'po_history.csv, demand.csv (header row = field names).')

    def handle(self, *args, json_path=None, csv_dir=None, **options):
        if bool(json_path) == bool(csv_dir):
            raise CommandError('Give exactly one of --json or --csv-dir.')
        data = self.read_json(Path(json_path)) if json_path else self.read_csv_dir(Path(csv_dir))
        try:
            run = loaders.load_datasets(data, models.SyncRun.IMPORT)
        except loaders.DataLoadError as exc:
            raise CommandError(f'Import failed, nothing was changed: {exc}')
        for key, count in run.counts.items():
            self.stdout.write(f'  {key}: {count} rows')
        self.stdout.write(self.style.SUCCESS('Import complete.'))

    def read_json(self, path):
        if not path.is_file():
            raise CommandError(f'File not found: {path}')
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise CommandError(f'Invalid JSON in {path}: {exc}')
        if not isinstance(data, dict):
            raise CommandError('JSON must be an object keyed by dataset name.')
        return data

    def read_csv_dir(self, path):
        if not path.is_dir():
            raise CommandError(f'Directory not found: {path}')
        data = {}
        for dataset in loaders.DATASETS:
            file = path / f'{dataset}.csv'
            if file.is_file():
                with file.open(newline='', encoding='utf-8-sig') as fh:
                    data[dataset] = list(csv.DictReader(fh))
        if not data:
            raise CommandError(f'No dataset CSV files found in {path}.')
        return data
