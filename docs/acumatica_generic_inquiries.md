# Generic Inquiries needed by the MRP planner

The MRP planner reads data from Acumatica, read-only, through **Generic Inquiries (GIs) exposed over OData**. It never writes to Acumatica. Please create the GIs below.

## Setup for all GIs

- In each GI, tick **Expose via OData**.
- Set each **column caption exactly as shown in the "Column caption" column**, with no spaces. The OData field names come from these captions, and the planner checks for them on every sync.
- Use **base units** for all quantities. The planner does not convert units of measure.
- Don't add GI parameters. Put conditions on the **Conditions** tab so the GI returns only the rows described.
- Create a **read-only service user** with access to these GIs only. Send its username and password to whoever maintains the planner. Don't email the password; pass it on in person or through a password manager. It goes in the planner's private `.env` file.
- The GI names can be changed. If you use different names, tell us so we can set them in `.env` (e.g. `ACUMATICA_GI_BOM=My BOM Inquiry`).

The "Typical source" column suggests where the data usually lives. Table and field names vary slightly between Acumatica versions, so please check them against yours.

When a GI is ready, you can test it in a browser (sign in as the service user):
`https://<your-site>/odata/<Tenant Name>/MRP-Vendors?$top=5&$format=json`

---

## 1. MRP-Vendors

Active vendors and their default purchasing lead time.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `VendorID` | yes | Vendor ID | Vendor.AcctCD |
| `VendorName` | yes | Vendor name | Vendor.AcctName |
| `LeadTimeDays` | no | Default lead time in days | Vendors screen → Purchase Settings → Lead Time |
| `Email` | no | Ordering contact email | Vendor primary contact email |

Condition: Vendor status = Active.

## 2. MRP-StockItems

Active stock items: purchased components and finished goods.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Item SKU | InventoryItem.InventoryCD |
| `Description` | no | Item description | InventoryItem.Descr |
| `ItemClass` | no | Item class | InventoryItem.ItemClassID |
| `IsFinishedGood` | yes | True for finished goods (manufactured/sold items), false for purchased components | Calculated column, e.g. `=IIf([InventoryItem.ItemClassID]='FINISHED', 1, 0)`, or based on Replenishment Source = Manufacturing |
| `BaseUnit` | no | Base unit of measure | InventoryItem.BaseUnit |
| `LotSize` | no | Order multiple / lot size (base units) | Item-warehouse or replenishment settings |

Condition: Item status = Active, stock items only.

## 3. MRP-OnHand

On-hand quantity by item and warehouse.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Item SKU | InventoryItem.InventoryCD |
| `Warehouse` | yes | Warehouse ID | INSite.SiteCD |
| `QtyOnHand` | yes | Quantity on hand (base units) | INSiteStatus (or INSiteStatusByCostCenter).QtyOnHand |

Condition: only warehouses that count as usable stock, and QtyOnHand ≠ 0.

## 4. MRP-OpenPOLines

Open purchase order lines that have not been fully received.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `OrderNbr` | yes | PO number | POLine.OrderNbr |
| `LineNbr` | yes | PO line number | POLine.LineNbr |
| `InventoryID` | yes | Item SKU | POLine.InventoryID |
| `VendorID` | yes | Vendor ID | POOrder.VendorID |
| `OpenQty` | yes | Quantity still to receive (base units) | POLine.BaseOpenQty (Order Qty − Received Qty) |
| `PromisedDate` | no | Promised date | POLine.PromisedDate |

Conditions: order type Normal, PO status Open, line not completed or cancelled, and OpenQty > 0.

## 5. MRP-BOM

Material lines of the **active** BOM revision for each finished good.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `FinishedGood` | yes | Finished-good SKU (the BOM item) | AMBomItem.InventoryID |
| `Component` | yes | Component SKU | AMBomMatl.InventoryID |
| `QtyPerUnit` | yes | Component quantity per **one** finished good (base units) | AMBomMatl.QtyReq ÷ batch size, adjusted for scrap if you use it |

Conditions: only the default/active BOM and revision per item, and only material lines (no subcontract or overhead lines). If a finished good has several BOMs, include only the default one.

## 6. MRP-ItemVendors

Item-vendor settings (Stock Items screen → Vendors tab).

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Item SKU | POVendorInventory.InventoryID |
| `VendorID` | yes | Vendor ID | POVendorInventory.VendorID |
| `IsDefault` | yes | True for the item's default vendor | POVendorInventory.IsDefault |
| `LeadTimeDays` | no | Item-specific lead time in days; leave blank to use the vendor default | Vendor lead time on the item-vendor line |
| `LastCost` | no | Last purchase price per base unit | POVendorInventory.LastPrice (converted to base unit) |
| `LotSize` | no | Purchase lot size / order multiple (base units) | POVendorInventory.LotSize |

Condition: only active vendors.

## 7. MRP-POHistory

Purchase order lines from the last 24 months. The planner uses these for the "Match last PO" quantity and the "Last PO" column.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Item SKU | POLine.InventoryID |
| `VendorID` | yes | Vendor ID | POOrder.VendorID |
| `OrderNbr` | yes | PO number | POLine.OrderNbr |
| `OrderDate` | yes | PO date | POOrder.OrderDate |
| `OrderQty` | yes | Ordered quantity (base units) | POLine.BaseOrderQty |
| `UnitCost` | no | Unit cost per base unit | POLine.CuryUnitCost |

Conditions: order type Normal, not cancelled, and OrderDate within the last 24 months.

## 8. Demand for finished goods

The planner needs monthly demand for each finished good. It can use any of three sources, and you choose which in the planner. Only the GI for the chosen source is needed. **Trailing shipments is the default**, so start with 8a.

### 8a. MRP-DemandShipments (default)

Confirmed shipment lines for finished goods, last 12 months. The planner averages the last 6 full months.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Finished-good SKU | SOShipLine.InventoryID |
| `ShipmentDate` | yes | Shipment date | SOShipment.ShipDate |
| `ShippedQty` | yes | Shipped quantity (base units) | SOShipLine.BaseShippedQty |

Conditions: shipment confirmed, operation Issue (exclude returns), and finished goods only.

### 8b. MRP-DemandSalesOrders (optional)

Open sales order lines for finished goods.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Finished-good SKU | SOLine.InventoryID |
| `RequestDate` | yes | Requested date | SOLine.RequestDate |
| `OpenQty` | yes | Open quantity (base units) | SOLine.BaseOpenQty |

Conditions: order open, line not completed, OpenQty > 0.

### 8c. MRP-DemandForecast (optional)

Forecast quantity per finished good per period.

| Column caption | Required | Description | Typical source |
|---|---|---|---|
| `InventoryID` | yes | Finished-good SKU | AMForecast.InventoryID (Manufacturing), or your own forecast table |
| `PeriodStart` | yes | First day of the forecast period | AMForecast.BeginDate |
| `ForecastQty` | yes | Forecast quantity (base units) | AMForecast.Qty |

---

## Already existing

westridgeApp already reads a GI called `LR-Replenishment`. If it already has some of these columns, tell us. We can reuse it rather than duplicate it.
