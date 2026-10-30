"""Pull every dataset from Acumatica Generic Inquiries and load it locally."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from acumatica.gi_specs import DEMAND_DATASETS, SPECS, map_rows
from acumatica.odata import AcumaticaConfig, ODataClient

from . import loaders, models

log = logging.getLogger('mrp')

# A running sync older than this is considered dead and no longer blocks a new one
STALE_AFTER = dt.timedelta(minutes=30)

MASTER_DATASETS = ['vendors', 'stock_items', 'on_hand', 'open_po_lines', 'bom', 'item_vendors', 'po_history']


class SyncInProgress(Exception):
    pass


def acumatica_config() -> AcumaticaConfig:
    cfg = settings.ACUMATICA
    return AcumaticaConfig(base_url=cfg['BASE_URL'], tenant=cfg['TENANT'], username=cfg['USERNAME'],
                           password=cfg['PASSWORD'], timeout=cfg['TIMEOUT'])


def acumatica_configured() -> bool:
    cfg = settings.ACUMATICA
    return all(cfg[k] for k in ('BASE_URL', 'TENANT', 'USERNAME', 'PASSWORD'))


def fetch_all(client: ODataClient, demand_sources: list[str]) -> dict[str, list[dict]]:
    gi_names = settings.ACUMATICA['GI_NAMES']
    data: dict[str, list[dict]] = {}
    for key in MASTER_DATASETS:
        spec = SPECS[key]
        data[key] = map_rows(spec, client.fetch_rows(gi_names[key], select=spec.select))
        log.info('Fetched %s: %d rows', key, len(data[key]))
    data['demand'] = []
    for source in demand_sources:
        spec = SPECS[DEMAND_DATASETS[source]]
        rows = map_rows(spec, client.fetch_rows(gi_names[spec.key], select=spec.select))
        data['demand'] += loaders.bucket_dated_demand(rows, source)
        log.info('Fetched demand %s: %d rows', source, len(rows))
    return data


def _start_run(user) -> models.SyncRun:
    with transaction.atomic():
        running = (models.SyncRun.objects.select_for_update()
                   .filter(source=models.SyncRun.ACUMATICA, status=models.SyncRun.RUNNING,
                           started_at__gte=timezone.now() - STALE_AFTER))
        if running.exists():
            raise SyncInProgress('A sync from Acumatica is already running.')
        return models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA, triggered_by=user)


def run_sync(user=None, demand_sources: Optional[list[str]] = None,
             client: Optional[ODataClient] = None) -> models.SyncRun:
    """Fetch everything first, then load it in one transaction."""
    if demand_sources is None:
        demand_sources = [models.PlanSettings.load().demand_source]
    run = _start_run(user)
    try:
        client = client or ODataClient(acumatica_config())
        data = fetch_all(client, demand_sources)
    except Exception as exc:
        loaders.fail_run(run, exc)
        raise
    return loaders.load_datasets(data, models.SyncRun.ACUMATICA, user=user, run=run)
