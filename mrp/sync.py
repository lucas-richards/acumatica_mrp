"""Pull planning data from Acumatica (REST API, read-only) and load it locally."""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
from pathlib import Path
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


def _process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # exists but belongs to another user
        return True
    return True


def clear_dead_runs() -> None:
    """Mark Acumatica runs as failed if their process is gone or they are too old."""
    for run in models.SyncRun.objects.filter(source=models.SyncRun.ACUMATICA, status=models.SyncRun.RUNNING):
        too_old = run.started_at < timezone.now() - STALE_AFTER
        if too_old or (run.pid and not _process_alive(run.pid)):
            loaders.fail_run(run, Exception('Interrupted: the sync process stopped before finishing.'))


def running_sync() -> Optional[models.SyncRun]:
    clear_dead_runs()
    return models.SyncRun.objects.filter(source=models.SyncRun.ACUMATICA, status=models.SyncRun.RUNNING).first()


def _start_run(user) -> models.SyncRun:
    clear_dead_runs()
    with transaction.atomic():
        running = (models.SyncRun.objects.select_for_update()
                   .filter(source=models.SyncRun.ACUMATICA, status=models.SyncRun.RUNNING))
        if running.exists():
            raise SyncInProgress('A sync from Acumatica is already running.')
        return models.SyncRun.objects.create(source=models.SyncRun.ACUMATICA, triggered_by=user, pid=os.getpid())


def start_background_sync(user) -> None:
    """Run sync_acumatica in its own process so a long sync never runs inside a web request
    (and survives a reload of the web process). Progress is read from SyncRun."""
    if running_sync():
        raise SyncInProgress('A sync from Acumatica is already running.')
    log_dir = settings.BASE_DIR / 'logs'
    log_dir.mkdir(exist_ok=True)
    python = Path(sys.prefix) / 'bin' / 'python'
    with open(log_dir / 'refresh.log', 'ab') as log:
        subprocess.Popen(
            [str(python), str(settings.BASE_DIR / 'manage.py'), 'sync_acumatica', '--user-id', str(user.pk)],
            cwd=settings.BASE_DIR, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )


def run_sync(user=None, demand_sources: Optional[list[str]] = None, client=None) -> models.SyncRun:
    """Fetch everything first (one logged-in session), then load it in one transaction."""
    if demand_sources is None:
        demand_sources = [models.PlanSettings.load().demand_source]
    options = FetchOptions(today=services.today(), demand_sources=demand_sources,
                           sales_order_types=tuple(settings.ACUMATICA['SALES_ORDER_TYPES']),
                           available_gi=settings.ACUMATICA['AVAILABLE_GI'],
                           receiving_gi=settings.ACUMATICA.get('RECEIVING_GI', FetchOptions.receiving_gi),
                           receiving_location=settings.ACUMATICA.get('RECEIVING_LOCATION',
                                                                      FetchOptions.receiving_location),
                           item_planning_gi=settings.ACUMATICA.get('ITEM_PLANNING_GI', FetchOptions.item_planning_gi))
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
