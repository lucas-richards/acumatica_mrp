"""Load datasets (from Acumatica, a JSON/CSV file or the sample data) into the local tables.

Every load is a full refresh of the datasets it contains, inside one transaction,
so a failed load leaves the previous data untouched.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from django.db import transaction
from django.utils import timezone

from planning.demand import DEMAND_SOURCES, month_start

from . import models


class DataLoadError(Exception):
    pass


def _str(v):
    return '' if v is None else str(v).strip()


def _opt_str(v):
    return _str(v)


def _decimal(v):
    if v is None or v == '':
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v).strip())
    except InvalidOperation:
        raise ValueError(f'not a number: {v!r}') from None


def _int(v):
    d = _decimal(v)
    return None if d is None else int(d)


def _date(v):
    if v is None or v == '':
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        raise ValueError(f'not a date (use YYYY-MM-DD): {v!r}') from None


def _bool(v):
    if isinstance(v, bool):
        return v
    return _str(v).lower() in ('true', '1', 'yes', 'y')


def _kind(v):
    v = _str(v).lower()
    if v in ('component', 'finished_good'):
        return v
    raise ValueError("kind must be 'component' or 'finished_good'")


def _source(v):
    v = _str(v)
    if v not in DEMAND_SOURCES:
        raise ValueError(f'source must be one of {", ".join(DEMAND_SOURCES)}')
    return v


# dataset -> (model, {field: (converter, required)})
DATASETS: dict[str, tuple[type, dict[str, tuple[Any, bool]]]] = {
    'vendors': (models.Vendor, {
        'vendor_id': (_str, True), 'name': (_str, True), 'lead_time_days': (_int, False), 'email': (_opt_str, False)}),
    'stock_items': (models.StockItem, {
        'sku': (_str, True), 'description': (_opt_str, False), 'item_class': (_opt_str, False),
        'kind': (_kind, True), 'uom': (_opt_str, False), 'lot_size': (_decimal, False)}),
    'on_hand': (models.OnHand, {'sku': (_str, True), 'warehouse': (_str, True), 'qty': (_decimal, True)}),
    'open_po_lines': (models.OpenPOLine, {
        'order_nbr': (_str, True), 'line_nbr': (_int, True), 'sku': (_str, True), 'vendor_id': (_str, True),
        'qty_open': (_decimal, True), 'promised_date': (_date, False)}),
    'bom': (models.BomLine, {'fg_sku': (_str, True), 'component_sku': (_str, True), 'qty_per': (_decimal, True)}),
    'item_vendors': (models.ItemVendor, {
        'sku': (_str, True), 'vendor_id': (_str, True), 'is_default': (_bool, False),
        'lead_time_days': (_int, False), 'last_cost': (_decimal, False), 'lot_size': (_decimal, False)}),
    'po_history': (models.PoHistory, {
        'sku': (_str, True), 'vendor_id': (_str, True), 'order_nbr': (_str, True), 'order_date': (_date, True),
        'qty': (_decimal, True), 'unit_cost': (_decimal, False)}),
    'demand': (models.DemandBucket, {
        'sku': (_str, True), 'source': (_source, True), 'month': (_date, True), 'qty': (_decimal, True)}),
}

LOAD_ORDER = list(DATASETS)


def clean_rows(dataset: str, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if dataset not in DATASETS:
        raise DataLoadError(f'Unknown dataset {dataset!r}; expected one of {", ".join(DATASETS)}')
    _, fields = DATASETS[dataset]
    cleaned = []
    for index, row in enumerate(rows, start=1):
        item = {}
        for name, (convert, required) in fields.items():
            raw = row.get(name)
            try:
                value = convert(raw)
            except ValueError as exc:
                raise DataLoadError(f'{dataset} row {index}, {name}: {exc}') from None
            if required and value in (None, ''):
                raise DataLoadError(f'{dataset} row {index}: {name} is required')
            item[name] = value
        if dataset == 'demand':
            item['month'] = month_start(item['month'])
        cleaned.append(item)
    if dataset == 'demand':
        cleaned = _merge_demand(cleaned)
    return cleaned


def _merge_demand(rows):
    totals: dict[tuple, Decimal] = defaultdict(Decimal)
    for r in rows:
        totals[(r['sku'], r['source'], r['month'])] += r['qty']
    return [{'sku': s, 'source': src, 'month': m, 'qty': q} for (s, src, m), q in totals.items()]


def _replace(dataset: str, rows: list[dict[str, Any]]) -> int:
    model, _ = DATASETS[dataset]
    if dataset == 'demand':
        # Replace only the demand sources present in this load
        sources = {r['source'] for r in rows}
        model.objects.filter(source__in=sources).delete()
    else:
        model.objects.all().delete()
    model.objects.bulk_create([model(**r) for r in rows], batch_size=1000)
    return len(rows)


def fail_run(run: models.SyncRun, error: Exception) -> None:
    run.status = models.SyncRun.FAILED
    run.error = str(error)
    run.finished_at = timezone.now()
    run.save()


def load_datasets(data: Mapping[str, Iterable[Mapping[str, Any]]], source: str, user=None,
                  run: models.SyncRun | None = None) -> models.SyncRun:
    """Validate and load every dataset present in `data`; records a SyncRun either way."""
    run = run or models.SyncRun.objects.create(source=source, triggered_by=user)
    try:
        cleaned = {key: clean_rows(key, data[key]) for key in LOAD_ORDER if key in data}
        unknown = set(data) - set(DATASETS) - {'anchor_date', '_comment'}
        if unknown:
            raise DataLoadError(f'Unknown dataset(s): {", ".join(sorted(unknown))}')
        if not cleaned:
            raise DataLoadError('No datasets found to load.')
        with transaction.atomic():
            counts = {key: _replace(key, rows) for key, rows in cleaned.items()}
    except Exception as exc:
        fail_run(run, exc)
        raise
    run.status = models.SyncRun.OK
    run.counts = counts
    run.finished_at = timezone.now()
    run.save()
    return run
