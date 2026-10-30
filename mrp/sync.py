"""Pull planning data from Acumatica (REST API, read-only) and load it locally."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from acumatica.entities import FetchOptions, fetch_all
from acumatica.rest import AcumaticaConfig, RestClient

from . import loaders, models, services

log = logging.getLogger('mrp')

# A running sync older than this is considered dead and no longer blocks a new one
STALE_AFTER = dt.timedelta(minutes=30)


class SyncInProgress(Exception):
    pass


def acumatica_config() -> AcumaticaConfig:
    cfg = settings.ACUMATICA
    return AcumaticaConfig(
        base_url=cfg['BASE_URL'], tenant=cfg['TENANT'], branch=cfg['BRANCH'],
        username=cfg['USERNAME'], password=cfg['PASSWORD'], timeout=cfg['TIMEOUT'],
        endpoints=(('default', cfg['ENDPOINT']), ('manufacturing', cfg['MANUFACTURING_ENDPOINT'])),
    )


def acumatica_configured() -> bool:
    cfg = settings.ACUMATICA
    return all(cfg[k] for k in ('BASE_URL', 'TENANT', 'USERNAME', 'PASSWORD'))


def _start_run(user) -> models.SyncRun:
    with transaction.atomic():
        running = (models.SyncRun.objects.select_for_update()
                   .filter(source=models.SyncRun.ACUMATICA, status=models.SyncRun.RUNNING,
                           started_at__gte=timezone.now() - STALE_AFTER))
        if running.exists():
            raise SyncInProgress('A sync from Acumatica is already running.')
        return models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA, triggered_by=user)


def run_sync(user=None, demand_sources: Optional[list[str]] = None, client=None) -> models.SyncRun:
    """Fetch everything first (one logged-in session), then load it in one transaction."""
    if demand_sources is None:
        demand_sources = [models.PlanSettings.load().demand_source]
    options = FetchOptions(today=services.today(), demand_sources=demand_sources,
                           sales_order_types=tuple(settings.ACUMATICA['SALES_ORDER_TYPES']))
    run = _start_run(user)
    try:
        if client is None:
            with RestClient(acumatica_config()) as rest:
                result = fetch_all(rest, options)
        else:
            result = fetch_all(client, options)
    except Exception as exc:
        loaders.fail_run(run, exc)
        raise
    run.warnings = result.warnings
    # Demand rows carry dates in "month"; the loader buckets them into calendar months.
    return loaders.load_datasets(result.data, models.SyncRun.ACUMATICA, user=user, run=run)
