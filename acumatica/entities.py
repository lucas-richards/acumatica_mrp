"""Read Acumatica entities over REST and map them to the planner's import format.

Datasets produced (same shapes as sample_data/sample_data.json):
vendors, stock_items, on_hand, open_po_lines, bom, item_vendors, po_history,
plus dated demand lines per demand source.

Rules:
- Only items in an active BOM tree are kept. Items with a BOM (finished goods
  and subassemblies) are "finished_good"; leaf materials are "component".
- Multi-level BOMs are exploded to purchased leaf components, so a finished
  good's qty-per includes components inside its subassemblies. Each subassembly
  also keeps its own lines.
- All quantities and costs are converted to the item's base unit using the
  item's own unit conversions; lines whose unit can't be converted are skipped
  and reported as warnings.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Protocol

from planning.demand import ASSEMBLY_SOURCE, ISSUE_SOURCE, OPEN_PO_SOURCE, OPEN_SO_SOURCE
from planning.inputs import is_excluded

from .rest import AcumaticaError, AcumaticaPermissionError

log = logging.getLogger('acumatica')

OPEN_PO_STATUSES = ('Open', 'Pending Printing', 'Pending Email')
STOCK_PO_LINE_TYPES = ('Goods for IN', 'Goods for Inventory', 'Goods for Manufacturing', 'Goods for SO')
INVOICE_STATUSES = ('Open', 'Closed')
OPEN_PRODUCTION_STATUSES = ('Planned', 'Released', 'In Process')
DONE_PRODUCTION_STATUSES = ('Completed', 'Closed')
OPEN_SO_STATUSES = ('Open', 'Back Order')
MAX_BOM_DEPTH = 12

DEMAND_SOURCE_ENTITIES = {
    'trailing_invoices': 'SalesInvoice',
    'open_sales_orders': 'SalesOrder',
    'forecast': 'Forecast',
}


class Client(Protocol):
    def fetch(self, endpoint: str, entity: str, *, select=None, expand=None, filter: str = '') -> list[dict]: ...

    def generic_inquiry(self, name: str, select=None) -> list[dict]: ...


@dataclass
class FetchOptions:
    today: dt.date
    demand_sources: list[str]
    po_history_months: int = 24
    shipment_months: int = 25      # 24 full months of history plus the current one
    sales_order_types: tuple[str, ...] = ('SO',)
    available_gi: str = 'LR-Available Qty'
    receiving_gi: str = 'DB-StorageDetailsByItemWarehouseLocation'
    receiving_location: str = 'REC-INS'
    item_planning_gi: str = 'IN-StockItem'


@dataclass
class FetchResult:
    data: dict[str, list[dict]]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dec(value: Any) -> Optional[Decimal]:
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def to_date(value: Any) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def odata_date(day: dt.date) -> str:
    return f"datetimeoffset'{day.isoformat()}T00:00:00Z'"


def months_ago(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) - months
    return dt.date(index // 12, index % 12 + 1, 1)


def any_of(field_name: str, values: Iterable[str]) -> str:
    return '(' + ' or '.join(f"{field_name} eq '{v}'" for v in values) + ')'


class UnitIssues:
    """Collects lines skipped for an unknown unit, reported once per (document type, item, unit)."""

    def __init__(self):
        self.seen: dict[tuple[str, str, str], list] = {}

    def add(self, kind: str, sku: str, uom: Optional[str], ref: Any) -> None:
        entry = self.seen.setdefault((kind, sku, uom or ''), [0, ref])
        entry[0] += 1

    def messages(self) -> list[str]:
        return [f'{kind}: {count} line(s) for {sku} in unit {uom} skipped — no conversion to the base unit '
                f'(e.g. {ref}). Add the conversion on the stock item in Acumatica.'
                for (kind, sku, uom), (count, ref) in sorted(self.seen.items())]


class Units:
    """Per-item unit conversions to the base unit."""

    def __init__(self):
        self.base: dict[str, str] = {}
        self.factors: dict[str, dict[str, Decimal]] = {}

    def add_item(self, sku: str, base_uom: str, conversions: Iterable[dict]) -> None:
        self.base[sku] = base_uom or ''
        factors = {(base_uom or '').upper(): Decimal(1)}
        for c in conversions or []:
            to_uom = (c.get('ToUOM') or '').upper()
            from_uom = (c.get('FromUOM') or '').upper()
            factor = dec(c.get('ConversionFactor'))
            if to_uom != (base_uom or '').upper() or not from_uom or not factor:
                continue
            factors[from_uom] = factor if (c.get('MultiplyOrDivide') or 'Multiply') == 'Multiply' else Decimal(1) / factor
        self.factors[sku] = factors

    def factor(self, sku: str, uom: Optional[str]) -> Optional[Decimal]:
        """Base units per one `uom`; None if unknown. A blank unit means the base unit."""
        if sku not in self.factors:
            return None
        if not uom:
            return Decimal(1)
        return self.factors[sku].get(uom.upper())

    def qty(self, sku: str, qty: Any, uom: Optional[str]) -> Optional[Decimal]:
        q, f = dec(qty), self.factor(sku, uom)
        return None if q is None or f is None else q * f


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

def fetch_boms(client: Client, today: dt.date) -> list[dict]:
    try:
        return client.fetch(
            'manufacturing', 'BillOfMaterial',
            filter="Status eq 'Active'",
            expand=['Operations/Material'],
            select=['BOMID', 'Revision', 'InventoryID', 'Status', 'EffStartDate', 'EffEndDate',
                    'Operations/OperationNbr', 'Operations/Material/InventoryID', 'Operations/Material/QtyRequired',
                    'Operations/Material/BatchSize', 'Operations/Material/UOM',
                    'Operations/Material/EffectiveDate', 'Operations/Material/ExpirationDate'],
        )
    except AcumaticaPermissionError as exc:
        raise AcumaticaPermissionError(
            'Cannot read Bills of Material. Ask your Acumatica admin to give the planner user access to the '
            f'Bill of Material screen (AM208000). Details: {exc}', exc.status_code) from exc


def choose_boms(boms: list[dict], today: dt.date, warnings: list[str]) -> dict[str, dict]:
    """One BOM per item: the active revision in effect today with the latest start date."""
    by_item: dict[str, list[dict]] = defaultdict(list)
    for bom in boms:
        start, end = to_date(bom.get('EffStartDate')), to_date(bom.get('EffEndDate'))
        if (start is None or start <= today) and (end is None or end >= today) and bom.get('InventoryID'):
            by_item[bom['InventoryID'].strip()].append(bom)
    chosen = {}
    for sku, candidates in by_item.items():
        candidates.sort(key=lambda b: (to_date(b.get('EffStartDate')) or dt.date.min, b.get('BOMID') or '',
                                       b.get('Revision') or ''), reverse=True)
        ids = {b.get('BOMID') for b in candidates}
        if len(ids) > 1:
            warnings.append(f'{sku} has {len(ids)} active BOMs ({", ".join(sorted(filter(None, ids)))}); '
                            f'using {candidates[0].get("BOMID")} rev {candidates[0].get("Revision")}.')
        chosen[sku] = candidates[0]
    return chosen


def direct_lines(bom: dict, today: dt.date) -> list[tuple[str, Decimal, Optional[str]]]:
    """(component, qty per one parent in material UOM, UOM) for a BOM, summed over operations."""
    totals: dict[tuple[str, Optional[str]], Decimal] = defaultdict(Decimal)
    for op in bom.get('Operations') or []:
        for m in op.get('Material') or []:
            sku = (m.get('InventoryID') or '').strip()
            qty = dec(m.get('QtyRequired'))
            if not sku or not qty:
                continue
            eff, exp = to_date(m.get('EffectiveDate')), to_date(m.get('ExpirationDate'))
            if (eff and eff > today) or (exp and exp < today):
                continue
            batch = dec(m.get('BatchSize')) or Decimal(1)
            totals[(sku, m.get('UOM'))] += qty / batch
    return [(sku, qty, uom) for (sku, uom), qty in totals.items()]


def explode(direct: dict[str, list[tuple[str, Decimal]]], warnings: list[str]) -> dict[str, dict[str, Decimal]]:
    """Leaf component quantities per one unit of every BOM parent (multi-level)."""
    memo: dict[str, dict[str, Decimal]] = {}

    def walk(item: str, path: tuple[str, ...]) -> dict[str, Decimal]:
        if item in memo:
            return memo[item]
        if item in path or len(path) > MAX_BOM_DEPTH:
            warnings.append(f'BOM loop or excessive depth at {" > ".join(path + (item,))}; branch skipped.')
            return {}
        leaves: dict[str, Decimal] = defaultdict(Decimal)
        for child, qty in direct.get(item, []):
            if child in direct:
                for leaf, leaf_qty in walk(child, path + (item,)).items():
                    leaves[leaf] += qty * leaf_qty
            else:
                leaves[child] += qty
        memo[item] = dict(leaves)
        return memo[item]

    return {parent: walk(parent, ()) for parent in direct}


def replenishment_source(item: dict) -> str:
    """Source from the Inventory Planning tab: what each warehouse of the item says ('Purchase',
    'Manufacturing', 'Kit Assembly', ...), 'Mixed' when they disagree, or '' when not set. Items
    without warehouse settings fall back to their replenishment class."""
    sources = {(w.get('ReplenishmentSource') or '').strip() for w in item.get('WarehouseDetails') or []} - {''}
    if not sources:
        sources = {(p.get('Source') or '').strip() for p in item.get('ReplenishmentParameters') or []} - {''}
    if len(sources) > 1:
        return 'Mixed'
    return sources.pop() if sources else ''


def fetch_available(client: Client, gi_name: str, warnings: list[str]) -> Optional[dict[str, Decimal]]:
    """Available quantity per item (summed over warehouses, base unit) from a Generic Inquiry
    with SKU, Warehouse and Available columns. None if it can't be read; the planner then
    uses quantity on hand."""
    try:
        rows = client.generic_inquiry(gi_name, select=['SKU', 'Warehouse', 'Available'])
    except AcumaticaError as exc:
        warnings.append(f'Available quantity could not be read from GI {gi_name!r} ({exc}); '
                        'the planner uses quantity on hand instead.')
        return None
    per_warehouse: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        sku = (row.get('SKU') or '').strip()
        if sku:
            # The GI can repeat an item/warehouse row (joined documents); the value is the same
            per_warehouse[(sku, (row.get('Warehouse') or '').strip())] = dec(row.get('Available')) or Decimal(0)
    available: dict[str, Decimal] = defaultdict(Decimal)
    for (sku, _), qty in per_warehouse.items():
        available[sku] += qty
    return dict(available)


def fetch_item_planning(client: Client, gi_name: str, warnings: list[str]) -> Optional[dict[str, dict[str, str]]]:
    """Planning method and replenishment source of each stock item as shown on the Inventory Planning
    settings, from a Generic Inquiry with InventoryID, PlanningMethod and Source columns. The REST API
    cannot report these correctly (it shows MRP as None), so this is the source of truth. None if the
    GI can't be read; the planner then falls back to the API source and does not use the planning method."""
    try:
        rows = client.generic_inquiry(gi_name, select=['InventoryID', 'PlanningMethod', 'Source'])
    except AcumaticaError as exc:
        warnings.append(f'Planning method and source could not be read from GI {gi_name!r} ({exc}); '
                        'the source comes from the replenishment class and the planning method is not used.')
        return None
    planning: dict[str, dict[str, str]] = {}
    for row in rows:
        sku = (row.get('InventoryID') or '').strip()
        if sku:
            planning[sku] = {'planning_method': (row.get('PlanningMethod') or '').strip(),
                             'source': (row.get('Source') or '').strip()}
    return planning


def fetch_receiving(client: Client, gi_name: str, location: str,
                    warnings: list[str]) -> Optional[dict[str, Decimal]]:
    """Quantity on hand sitting in the receiving location (e.g. REC-INS), per item, summed over
    warehouses. Available quantity leaves it out. None if the GI can't be read."""
    try:
        rows = client.generic_inquiry(gi_name, select=['InventoryID', 'WarehouseID', 'LocationID',
                                                      'QtyOnHandinLocation'])
    except AcumaticaError as exc:
        warnings.append(f'Quantity in receiving could not be read from GI {gi_name!r} ({exc}); '
                        'items just received are not counted.')
        return None
    wanted = location.strip().upper()
    per_warehouse: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        sku = (row.get('InventoryID') or '').strip()
        if sku and (row.get('LocationID') or '').strip().upper() == wanted:
            # The GI repeats a location row for each lot/serial or joined record; the quantity is the same
            per_warehouse[(sku, (row.get('WarehouseID') or '').strip())] = dec(row.get('QtyOnHandinLocation')) or Decimal(0)
    receiving: dict[str, Decimal] = defaultdict(Decimal)
    for (sku, _), qty in per_warehouse.items():
        receiving[sku] += qty
    return dict(receiving)


# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------

STOCK_ITEM_SELECT = [
    'InventoryID', 'Description', 'ItemClass', 'ItemType', 'ItemStatus', 'BaseUOM',
    'ReplenishmentParameters/Source', 'ReplenishmentParameters/Method',
    'UOMConversions/FromUOM', 'UOMConversions/ToUOM', 'UOMConversions/MultiplyOrDivide',
    'UOMConversions/ConversionFactor',
    'WarehouseDetails/WarehouseID', 'WarehouseDetails/QtyOnHand', 'WarehouseDetails/ReplenishmentSource',
    'VendorDetails/VendorID', 'VendorDetails/Default', 'VendorDetails/Active', 'VendorDetails/Warehouse',
    'VendorDetails/LeadTimeDays', 'VendorDetails/AddLeadTimeDays',
    'VendorDetails/LotSize', 'VendorDetails/PurchaseUnit',
]


def fetch_all(client: Client, options: FetchOptions) -> FetchResult:
    today = options.today
    warnings: list[str] = []
    data: dict[str, list[dict]] = {}

    # 1. BOMs (fail first and fast if access is missing)
    boms = choose_boms(fetch_boms(client, today), today, warnings)

    # 2. Stock items with units, on hand and vendor settings
    items = {(r.get('InventoryID') or '').strip(): r for r in client.fetch(
        'default', 'StockItem', filter="ItemStatus ne 'Inactive'",
        expand=['UOMConversions', 'WarehouseDetails', 'VendorDetails', 'ReplenishmentParameters'],
        select=STOCK_ITEM_SELECT) if not is_excluded(r.get('InventoryID'))}
    units = Units()
    for sku, item in items.items():
        units.add_item(sku, item.get('BaseUOM'), item.get('UOMConversions'))

    unit_issues = UnitIssues()

    # BOM lines in base units, then multi-level explosion. Materials that are not
    # active stock items (non-stock packaging, inactive parts) have no stock to plan.
    not_stock: set[str] = set()
    direct: dict[str, list[tuple[str, Decimal]]] = {}
    for parent, bom in boms.items():
        if is_excluded(parent):
            continue
        lines = []
        for child, qty, uom in direct_lines(bom, today):
            if is_excluded(child):
                continue
            if child not in items:
                not_stock.add(child)
                continue
            base_qty = units.qty(child, qty, uom)
            if base_qty is None:
                unit_issues.add('BOM', child, uom, parent)
                continue
            lines.append((child, base_qty))
        direct[parent] = lines
    exploded = explode(direct, warnings)
    parents = set(exploded)
    components = {c for leaves in exploded.values() for c in leaves}
    relevant = parents | components

    missing = relevant - set(items)
    not_stock |= missing
    relevant -= missing
    parents &= relevant
    if not_stock:
        names = sorted(not_stock)
        warnings.append(f'{len(names)} BOM item(s) are non-stock or inactive and are not planned: '
                        + ', '.join(names[:30]) + (' …' if len(names) > 30 else ''))
    data['bom'] = [{'fg_sku': p, 'component_sku': c, 'qty_per': q}
                   for p, leaves in exploded.items() if p in relevant
                   for c, q in leaves.items() if q > 0 and c in relevant]

    item_planning = fetch_item_planning(client, options.item_planning_gi, warnings) or {}

    def source_of(sku: str) -> str:
        return item_planning.get(sku, {}).get('source') or replenishment_source(items[sku])

    # Bought items that are in no BOM (e.g. a kit that is received and broken down) still need
    # purchasing: keep any active, non-manufactured item that has an active vendor line
    bought = {sku for sku, item in items.items()
              if sku not in relevant and source_of(sku) in ('Purchase', '')
              and any((vd.get('VendorID') or '').strip() and vd.get('Active') is not False
                      for vd in item.get('VendorDetails') or [])}
    parents |= bought
    relevant |= bought

    available = fetch_available(client, options.available_gi, warnings)
    receiving = fetch_receiving(client, options.receiving_gi, options.receiving_location, warnings)
    data['stock_items'] = [{
        'sku': sku,
        'description': items[sku].get('Description') or '',
        'item_class': items[sku].get('ItemClass') or '',
        'kind': 'finished_good' if sku in parents else 'component',
        'uom': items[sku].get('BaseUOM') or '',
        'lot_size': None,
        'qty_available': None if available is None else available.get(sku, Decimal(0)),
        'qty_receiving': None if receiving is None else receiving.get(sku, Decimal(0)),
        'item_type': items[sku].get('ItemType') or '',
        'replenishment_source': source_of(sku),
        'planning_method': item_planning.get(sku, {}).get('planning_method', ''),
    } for sku in sorted(relevant)]

    data['on_hand'] = [
        {'sku': sku, 'warehouse': w.get('WarehouseID') or '', 'qty': dec(w.get('QtyOnHand')) or Decimal(0)}
        for sku in sorted(relevant) for w in items[sku].get('WarehouseDetails') or []
        if dec(w.get('QtyOnHand'))
    ]

    data['item_vendors'] = []
    # Finished goods that are bought rather than built (kits that are received and broken down) are
    # planned like components, so their vendor settings are needed too
    purchased_fgs = bought | {sku for sku in parents if source_of(sku) == 'Purchase'}
    for sku in sorted(relevant & (components | purchased_fgs)):
        item = items[sku]
        per_vendor: dict[str, dict] = {}
        for vd in item.get('VendorDetails') or []:
            vendor = (vd.get('VendorID') or '').strip()
            if not vendor or vd.get('Active') is False:
                continue
            # Prefer the all-warehouses line over warehouse-specific ones
            if vendor in per_vendor and not per_vendor[vendor].get('Warehouse'):
                continue
            per_vendor[vendor] = vd
        for vendor, vd in per_vendor.items():
            purchase_unit = vd.get('PurchaseUnit')
            lead = vd.get('LeadTimeDays')
            lot = units.qty(sku, vd.get('LotSize'), purchase_unit) if dec(vd.get('LotSize')) else None
            data['item_vendors'].append({
                'sku': sku, 'vendor_id': vendor, 'is_default': bool(vd.get('Default')),
                'lead_time_days': None if lead is None else int(lead) + int(vd.get('AddLeadTimeDays') or 0),
                'lot_size': lot or None,
            })

    # 3. Vendors
    data['vendors'] = [{
        'vendor_id': (v.get('VendorID') or '').strip(), 'name': v.get('VendorName') or v.get('VendorID'),
        'lead_time_days': v.get('LeadTimedays') or None, 'email': '',
    } for v in client.fetch('default', 'Vendor', filter="Status eq 'Active'",
                            select=['VendorID', 'VendorName', 'Status', 'LeadTimedays'])]

    # 4. Purchase orders: open lines and history
    since = months_ago(today, options.po_history_months)
    pos = client.fetch(
        'default', 'PurchaseOrder',
        filter=f"Type eq 'Normal' and ({any_of('Status', OPEN_PO_STATUSES)[1:-1]} or Date ge {odata_date(since)})",
        expand=['Details'],
        select=['OrderNbr', 'Type', 'Status', 'Date', 'VendorID', 'Details/LineNbr', 'Details/LineType',
                'Details/InventoryID', 'Details/UOM', 'Details/OrderQty', 'Details/QtyOnReceipts',
                'Details/Promised', 'Details/Completed', 'Details/Cancelled'])
    data['open_po_lines'], data['po_history'] = [], []
    for po in pos:
        status, order_date = po.get('Status'), to_date(po.get('Date'))
        for line in po.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in relevant or line.get('LineType') not in STOCK_PO_LINE_TYPES:
                continue
            qty = units.qty(sku, line.get('OrderQty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Purchase order', sku, line.get('UOM'), po.get('OrderNbr'))
                continue
            if status in OPEN_PO_STATUSES and not line.get('Completed') and not line.get('Cancelled'):
                received = units.qty(sku, line.get('QtyOnReceipts') or 0, line.get('UOM')) or Decimal(0)
                open_qty = qty - received
                if open_qty > 0:
                    data['open_po_lines'].append({
                        'order_nbr': po.get('OrderNbr'), 'line_nbr': line.get('LineNbr'), 'sku': sku,
                        'vendor_id': po.get('VendorID'), 'qty_open': open_qty,
                        'promised_date': to_date(line.get('Promised')),
                        'order_date': order_date,
                    })
            # A line closed (Completed) with nothing received was dropped, not bought: it is not the last PO
            dropped = bool(line.get('Completed')) and not (line.get('QtyOnReceipts') or 0)
            if status != 'Canceled' and not line.get('Cancelled') and not dropped and order_date and order_date >= since:
                data['po_history'].append({
                    'sku': sku, 'vendor_id': po.get('VendorID'), 'order_nbr': po.get('OrderNbr'),
                    'order_date': order_date, 'qty': qty,
                })

    # 5. Finished-good demand
    data['demand'] = []
    dated: dict[str, list[dict]] = {}
    open_so = _sales_orders(client, options, relevant, units, unit_issues)   # also feeds the SO/PO column
    for source in options.demand_sources:
        if source == 'trailing_invoices':
            dated[source] = _invoices(client, options, parents, units, unit_issues)
        elif source == 'open_sales_orders':
            dated[source] = [r for r in open_so if r['sku'] in parents]
        elif source == 'forecast':
            raise AcumaticaError('The forecast demand source is not available from Acumatica yet '
                                 '(the planner user has no access to the Forecast screen AM202000).')
        else:
            raise AcumaticaError(f'Unknown demand source {source!r}')
    # Component usage comes from what was actually issued, whatever the finished-good source is
    dated[ISSUE_SOURCE] = _issues(client, options, components & relevant, units, unit_issues)
    dated[ASSEMBLY_SOURCE] = _assembly_out(client, options, relevant, units, unit_issues)
    data['last_production'] = _last_production(client, options, parents, units, unit_issues)
    dated[OPEN_SO_SOURCE] = open_so
    dated[OPEN_PO_SOURCE] = _open_production(client, options, relevant, units, unit_issues)
    for source, rows in dated.items():
        data['demand'] += [{'sku': r['sku'], 'source': source, 'month': r['date'], 'qty': r['qty']} for r in rows]

    warnings += unit_issues.messages()
    for w in warnings:
        log.warning(w)
    return FetchResult(data=data, warnings=warnings)


def _invoices(client, options, parents, units, unit_issues) -> list[dict]:
    """Invoiced quantity of finished goods by invoice date; credit memos count as negative."""
    since = months_ago(options.today, options.shipment_months)
    rows = []
    for inv in client.fetch(
            'default', 'SalesInvoice',
            filter=f"Date ge {odata_date(since)} and {any_of('Status', INVOICE_STATUSES)}",
            expand=['Details'],
            select=['ReferenceNbr', 'Type', 'Date', 'Status', 'Details/InventoryID', 'Details/Qty', 'Details/UOM']):
        day = to_date(inv.get('Date'))
        sign = -1 if inv.get('Type') == 'Credit Memo' else 1
        for line in inv.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in parents or not day:
                continue
            qty = units.qty(sku, line.get('Qty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Invoice', sku, line.get('UOM'), inv.get('ReferenceNbr'))
                continue
            rows.append({'sku': sku, 'date': day, 'qty': qty * sign})
    return rows


def _issues(client, options, components, units, unit_issues) -> list[dict]:
    """Released inventory issues of purchased components by date: the lines of type Issue (production
    material and other issues). Lines of type Invoice (components sold straight to a customer, often one
    bulk sale) and their Credit Memo returns are left out, so a one-off sale does not inflate usage."""
    since = months_ago(options.today, options.shipment_months)
    rows = []
    for doc in client.fetch(
            'default', 'InventoryIssue',
            filter=f"Date ge {odata_date(since)} and Status eq 'Released'",
            expand=['Details'],
            select=['ReferenceNbr', 'Date', 'Status', 'Details/InventoryID', 'Details/Qty', 'Details/UOM',
                    'Details/TranType']):
        day = to_date(doc.get('Date'))
        for line in doc.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in components or not day or line.get('TranType') in ('Invoice', 'Credit Memo'):
                continue
            qty = units.qty(sku, line.get('Qty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Inventory issue', sku, line.get('UOM'), doc.get('ReferenceNbr'))
                continue
            rows.append({'sku': sku, 'date': day, 'qty': qty})
    return rows


def _assembly_out(client, options, skus, units, unit_issues) -> list[dict]:
    """Quantity consumed by released kit assemblies: the components of an assembly, or the kit itself
    when it is disassembled. Components returned by a disassembly count as negative."""
    since = months_ago(options.today, options.shipment_months)
    rows = []

    def add(sku, qty, uom, day, ref, sign=1):
        sku = (sku or '').strip()
        if sku not in skus:
            return
        base = units.qty(sku, qty, uom)
        if base is None:
            unit_issues.add('Kit assembly', sku, uom, ref)
            return
        rows.append({'sku': sku, 'date': day, 'qty': base * sign})

    for doc in client.fetch(
            'default', 'KitAssembly',
            filter=f"Date ge {odata_date(since)} and Status eq 'Released'",
            expand=['StockComponents'],
            select=['ReferenceNbr', 'Date', 'Status', 'Type', 'KitInventoryID', 'Qty', 'UOM',
                    'StockComponents/StockInventoryID', 'StockComponents/Qty', 'StockComponents/UOM']):
        day = to_date(doc.get('Date'))
        if not day:
            continue
        ref = doc.get('ReferenceNbr')
        if doc.get('Type') == 'Disassembly':
            add(doc.get('KitInventoryID'), doc.get('Qty'), doc.get('UOM'), day, ref)
        for line in doc.get('StockComponents') or []:
            add(line.get('StockInventoryID'), line.get('Qty'), line.get('UOM'), day, ref,
                -1 if doc.get('Type') == 'Disassembly' else 1)
    return rows


def _open_production(client, options, skus, units, unit_issues) -> list[dict]:
    """Open production orders: the quantity still to produce, and the material still to be issued."""
    status = any_of('Status', OPEN_PRODUCTION_STATUSES)
    rows = []
    due: dict[str, dt.date] = {}   # order id / number -> due date
    for order in client.fetch(
            'manufacturing', 'ProductionOrder', filter=status,
            select=['ProductionNbr', 'InventoryID', 'Status', 'QtyRemaining', 'UOM', 'StartDate', 'EndDate']):
        day = to_date(order.get('EndDate')) or to_date(order.get('StartDate')) or options.today
        due[order.get('id') or order.get('ProductionNbr')] = day
        sku = (order.get('InventoryID') or '').strip()
        qty = units.qty(sku, order.get('QtyRemaining'), order.get('UOM')) if sku in skus else None
        if sku in skus and qty is None:
            unit_issues.add('Production order', sku, order.get('UOM'), order.get('ProductionNbr'))
        elif qty is not None and qty > 0:
            rows.append({'sku': sku, 'date': day, 'qty': qty})
    for order in client.fetch(
            'manufacturing', 'ProductionOrderDetail', filter=status, expand=['Operations/Material'],
            select=['ProductionNbr', 'Status', 'Operations/Material/InventoryID',
                    'Operations/Material/QtyRemaining', 'Operations/Material/UOM', 'Operations/Material/Byproduct']):
        day = due.get(order.get('id')) or due.get(order.get('ProductionNbr')) or options.today
        for operation in order.get('Operations') or []:
            for line in operation.get('Material') or []:
                sku = (line.get('InventoryID') or '').strip()
                if sku not in skus or line.get('Byproduct'):
                    continue
                qty = units.qty(sku, line.get('QtyRemaining'), line.get('UOM'))
                if qty is None:
                    unit_issues.add('Production material', sku, line.get('UOM'), order.get('ProductionNbr'))
                elif qty > 0:
                    rows.append({'sku': sku, 'date': day, 'qty': qty})
    return rows


def _last_production(client, options, fg_skus, units, unit_issues) -> list[dict]:
    """The most recent finished production order of each finished good: its quantity and end date."""
    since = months_ago(options.today, options.po_history_months)
    latest: dict[str, dict] = {}
    for order in client.fetch(
            'manufacturing', 'ProductionOrder',
            filter=f"{any_of('Status', DONE_PRODUCTION_STATUSES)} and EndDate ge {odata_date(since)}",
            select=['ProductionNbr', 'InventoryID', 'Status', 'QtytoProduce', 'UOM', 'EndDate']):
        sku = (order.get('InventoryID') or '').strip()
        day = to_date(order.get('EndDate'))
        if sku not in fg_skus or not day or order.get('Status') not in DONE_PRODUCTION_STATUSES:
            continue
        qty = units.qty(sku, order.get('QtytoProduce'), order.get('UOM'))
        if qty is None:
            unit_issues.add('Production order', sku, order.get('UOM'), order.get('ProductionNbr'))
        elif qty > 0 and (sku not in latest or day > latest[sku]['run_date']):
            latest[sku] = {'sku': sku, 'qty': qty, 'run_date': day}
    return sorted(latest.values(), key=lambda r: r['sku'])


def _sales_orders(client, options, relevant, units, unit_issues) -> list[dict]:
    rows = []
    for so in client.fetch(
            'default', 'SalesOrder',
            filter=f"{any_of('OrderType', options.sales_order_types)} and {any_of('Status', OPEN_SO_STATUSES)}",
            expand=['Details'],
            select=['OrderType', 'OrderNbr', 'Status', 'RequestedOn', 'Details/InventoryID', 'Details/OpenQty',
                    'Details/UOM', 'Details/RequestedOn', 'Details/Operation', 'Details/Completed']):
        for line in so.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in relevant or line.get('Completed') or line.get('Operation') not in (None, 'Issue'):
                continue
            day = to_date(line.get('RequestedOn')) or to_date(so.get('RequestedOn'))
            qty = units.qty(sku, line.get('OpenQty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Sales order', sku, line.get('UOM'), so.get('OrderNbr'))
                continue
            if day and qty > 0:
                rows.append({'sku': sku, 'date': day, 'qty': qty})
    return rows
