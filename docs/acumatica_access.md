# How the MRP planner reads Acumatica

The planner reads Acumatica's own records through the **contract-based REST API**, plus one existing Generic Inquiry (LR-Available Qty) for the available quantity. It never writes: it logs in, makes GET requests, and logs out.

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
| Invoices (default demand source for finished goods) | `SalesInvoice` | Invoices (SO303000) |
| Inventory issues (component usage) | `InventoryIssue` | Issues (IN302000) |
| Open sales orders (optional demand source) | `SalesOrder` | Sales Orders (SO301000) |
| Available quantity | Generic Inquiry `LR-Available Qty` (OData) | that GI |

The Forecast screen (AM202000) is not available to this user, so the **forecast** demand source can't be used with Acumatica yet.

## What is read, and how it's used

**Bills of material.** Only BOMs with status Active, whose effective dates include today, are read. If an item has more than one, the planner uses the one with the latest start date and reports a warning. Each material line counts as quantity required ÷ batch size, converted to the component's base unit. Lines outside their effective/expiration dates are ignored.

**Multi-level BOMs.**
- Every item that has a BOM, whether a finished good or a subassembly, is treated as a "finished good".
- Every material without its own BOM is a "component", meaning a purchased part.
- BOMs are exploded through subassemblies. For example, if FG-1 uses 2 × SA-1 and SA-1 uses 4 bolts, FG-1 needs 8 bolts from that route.
- Subassemblies keep their own lines too, so any invoices of them count as demand.

**Stock items.** All items except Inactive ones are read with:
- unit conversions, used to convert every quantity and price to the base unit
- on-hand quantity per warehouse
- vendor details

Only items that appear in a BOM are kept.

**Planning method and source.** Both come from the Generic Inquiry **IN-StockItem** over OData (columns `InventoryID`, `PlanningMethod`, `Source`; change the name with `ACUMATICA_ITEM_PLANNING_GI`), because the REST API cannot report them correctly (it shows MRP as None). If the inquiry can't be read, the sync records a warning, takes the source from the item's warehouse and replenishment class settings, and does not use the planning method.

**Purchased or manufactured.** The Source on each stock item decides whether an item belongs on the purchasing plan. Items made in-house, meaning Manufacturing or Kit Assembly, belong on the production planning screen and are left off:
- **Manufacturing**: made in-house, so it is left off the purchasing plan.
- **Purchase**: planned as normal.
- **Finished goods with Source = Purchase** (kits that are bought and then broken down into their parts): planned like components, using their default vendor, lead time and lot size. Their usage is invoices plus units consumed or disassembled by kit assemblies; inventory issues are not counted for them. They need a BOM (the kit parts) to be picked up.
- **Planning method None**: not planned, so left off the plan. MRP and Inventory Replenishment are planned.
- **Both**, under different replenishment classes: kept on the plan and flagged "Purchase or manufacture" so someone can check it.

The plan is one list of everything to purchase. A component's Impact column lists the finished goods it is used in, with each finished good's monthly demand, stock, months of stock (marked low at one month or less) and the quantity of its last completed production run (the most recent Completed or Closed production order of the last 24 months, using Qty to Produce). A bought finished good has no impact rows.

**Item-vendor settings** (Stock Items → Vendors tab, active lines only):
- **Lead time** = Lead Time (days) + Add. Lead Time (days). If blank, the vendor's lead time is used. The app does not override it.
- **Lot size** = Lot Size converted to the base unit. Blank or 0 means no rounding.
- The default vendor is the line marked Default. If an item has both an all-warehouse line and warehouse-specific lines for the same vendor, the all-warehouse line wins.

**Two-year average.** The "Avg / mo, 2 yrs" column uses the same measure as usage (issues and kit assemblies for components; invoices and kit assemblies for bought finished goods) averaged over the last 24 full months, and counts the months with any use. History is therefore read for the last 25 months. Compare it with usage per month to tell steady items from ones used in bursts.

**Available quantity.** Read from the existing Generic Inquiry **LR-Available Qty** over OData (columns `SKU`, `Warehouse`, `Available`; change the GI name with `ACUMATICA_AVAILABLE_GI`). Repeated item/warehouse rows are counted once and warehouses are added up. Available can be negative (back-ordered). If the GI can't be read, the planner uses quantity on hand and the page says so.

**In receiving.** Goods received into the receiving location (default `REC-INS`, Receiving Inspection) are not in the available quantity. They are read from the Generic Inquiry **DB-StorageDetailsByItemWarehouseLocation** over OData (columns `InventoryID`, `WarehouseID`, `LocationID`, `QtyOnHandinLocation`; change the names with `ACUMATICA_RECEIVING_GI` and `ACUMATICA_RECEIVING_LOCATION`), shown in the On order column as "in Receiving", and counted in the supply like an order that has arrived. If the GI can't be read, they are left out and the sync records a warning.

**Vendors.** Active vendors only, with their Lead Time (days).

**Purchase orders.** Type **Normal** only; drop-ship POs never add stock. Only lines of type "Goods for IN / Inventory / Manufacturing / SO" are used.
- *On order*: open POs (status Open, Pending Printing or Pending Email), lines not completed or cancelled. Open quantity is ordered − received, in base units. The page shows the date of the oldest open PO as "since".
- *History* (for "Last order"): lines from the last 24 months, excluding cancelled ones.

**Demand for finished goods**, chosen on the settings:
- *Trailing invoices* (default): finished-good lines on sales invoices with status Open or Closed from the last 25 months, credit memos subtracted. The planner averages the last 6 full months.
- *Component usage* always comes from released inventory issues of the last 25 months (lines of type Issue, such as production material issues; lines of type Invoice, meaning components sold straight to a customer, and their Credit Memo returns are left out so a one-off bulk sale does not inflate usage), averaged over the same window. A component with no issues has no usage. Open sales orders for a finished good, less the quantity still to be produced on its open production orders, are taken off the supply of the components it uses.
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
