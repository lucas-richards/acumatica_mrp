# How the MRP planner reads Acumatica

The planner reads Acumatica's own records through the **contract-based REST API**. It never writes: it logs in, makes GET requests, and logs out. No Generic Inquiries are needed.

- Endpoints: `Default/24.200.001` and `MANUFACTURING/24.200.001`. You can change these with `ACUMATICA_ENDPOINT` and `ACUMATICA_MANUFACTURING_ENDPOINT`.
- Login: the user in `.env` (`ACUMATICA_USERNAME` / `ACUMATICA_PASSWORD`), tenant `ACUMATICA_TENANT`, branch `ACUMATICA_BRANCH`.
- Each sync uses one API session, which is always logged out at the end, even after an error.

## Screen rights the Acumatica user needs (read-only is enough)

| Data | REST entity | Acumatica screen |
|---|---|---|
| Bills of material | `MANUFACTURING/BillOfMaterial` | Bill of Material (AM208000) |
| Stock items, unit conversions, on hand by warehouse, item-vendor settings | `StockItem` | Stock Items (IN202500) |
| Vendors | `Vendor` | Vendors (AP303000) |
| Purchase orders | `PurchaseOrder` | Purchase Orders (PO301000) |
| Shipments (default demand source) | `Shipment` | Shipments (SO302000) |
| Open sales orders (optional demand source) | `SalesOrder` | Sales Orders (SO301000) |
| Available quantity | `InventoryQuantityAvailable` inquiry | Inventory Quantity Available (SO640590): **not accessible yet**; until then the planner uses quantity on hand |

The Forecast screen (AM202000) is not available to this user, so the **forecast** demand source can't be used with Acumatica yet.

## What is read, and how it's used

**Bills of material.** Only BOMs with status Active, whose effective dates include today, are read. If an item has more than one, the planner uses the one with the latest start date and reports a warning. Each material line counts as quantity required ÷ batch size, converted to the component's base unit. Lines outside their effective/expiration dates are ignored.

**Multi-level BOMs.**
- Every item that has a BOM, whether a finished good or a subassembly, is treated as a "finished good".
- Every material without its own BOM is a "component", meaning a purchased part.
- BOMs are exploded through subassemblies. For example, if FG-1 uses 2 × SA-1 and SA-1 uses 4 bolts, FG-1 needs 8 bolts from that route.
- Subassemblies keep their own lines too, so their stock counts as "sitting in FG stock" and any shipments of them count as demand.

**Stock items.** All items except Inactive ones are read with:
- unit conversions, used to convert every quantity and price to the base unit
- on-hand quantity per warehouse
- vendor details

Only items that appear in a BOM are kept.

**Item-vendor settings** (Stock Items → Vendors tab, active lines only):
- **Lead time** = Lead Time (days) + Add. Lead Time (days). If blank, the vendor's lead time is used. A planner override in the app beats both.
- **Cost** = Last Vendor Price converted from the purchase unit to the base unit. If there's no price, the item's Last Cost is used.
- **Lot size** = Lot Size converted to the base unit. Blank or 0 means no rounding.
- The default vendor is the line marked Default. If an item has both an all-warehouse line and warehouse-specific lines for the same vendor, the all-warehouse line wins.

**Available quantity.** Read from the Inventory Quantity Available inquiry: one total per item across warehouses, in the base unit. Inquiries are run with a PUT of their filter values, which is how Acumatica runs an inquiry; nothing is changed. Until the user has access to that screen, the planner uses quantity on hand and the page says so.

**Vendors.** Active vendors only, with their Lead Time (days).

**Purchase orders.** Type **Normal** only; drop-ship POs never add stock. Only lines of type "Goods for IN / Inventory / Manufacturing / SO" are used.
- *On order*: open POs (status Open, Pending Printing or Pending Email), lines not completed or cancelled. Open quantity is ordered − received, in base units. The page shows the date of the oldest open PO as "since".
- *History* (for "Last order"): lines from the last 24 months, excluding cancelled ones.

**Demand for finished goods**, chosen on the settings:
- *Trailing shipments* (default): shipments of type Shipment, operation Issue and status Confirmed or Completed, from the last 13 months. The planner averages the last 6 full months.
- *Open sales orders*: order type `SO` (change with `ACUMATICA_SALES_ORDER_TYPES`) with status Open or Back Order. Open quantity on Issue lines counts, by requested date. Quotes, transfers and returns are excluded.

## Warnings

Problems that don't stop a sync are listed after the sync (command output, and the Sync runs page in the admin). Examples:
- a line in a unit the item has no conversion for
- an item with several active BOMs
- a BOM pointing to an inactive item

Each warning says what was skipped.

## Not used yet

- Production orders.
- Allocations to open production orders.
- Components sold directly as spare parts.

These are possible later improvements.
