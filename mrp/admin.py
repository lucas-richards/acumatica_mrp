from django.contrib import admin

from . import models


class ReadOnlyAdmin(admin.ModelAdmin):
    """Synced data and audit records are read-only here; change them in Acumatica or the app."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Vendor)
class VendorAdmin(ReadOnlyAdmin):
    list_display = ('vendor_id', 'name', 'lead_time_days', 'email')
    search_fields = ('vendor_id', 'name')


@admin.register(models.StockItem)
class StockItemAdmin(ReadOnlyAdmin):
    list_display = ('sku', 'description', 'kind', 'item_class', 'uom', 'lot_size')
    list_filter = ('kind', 'item_class')
    search_fields = ('sku', 'description')


@admin.register(models.OnHand)
class OnHandAdmin(ReadOnlyAdmin):
    list_display = ('sku', 'warehouse', 'qty')
    search_fields = ('sku',)


@admin.register(models.OpenPOLine)
class OpenPOLineAdmin(ReadOnlyAdmin):
    list_display = ('order_nbr', 'line_nbr', 'sku', 'vendor_id', 'qty_open', 'promised_date')
    search_fields = ('order_nbr', 'sku')


@admin.register(models.BomLine)
class BomLineAdmin(ReadOnlyAdmin):
    list_display = ('fg_sku', 'component_sku', 'qty_per')
    search_fields = ('fg_sku', 'component_sku')


@admin.register(models.ItemVendor)
class ItemVendorAdmin(ReadOnlyAdmin):
    list_display = ('sku', 'vendor_id', 'is_default', 'lead_time_days', 'lot_size')
    search_fields = ('sku', 'vendor_id')


@admin.register(models.LastProduction)
class LastProductionAdmin(ReadOnlyAdmin):
    list_display = ('sku', 'qty', 'run_date')
    search_fields = ('sku',)


@admin.register(models.PoHistory)
class PoHistoryAdmin(ReadOnlyAdmin):
    list_display = ('order_nbr', 'sku', 'vendor_id', 'order_date', 'qty')
    search_fields = ('order_nbr', 'sku')


@admin.register(models.DemandBucket)
class DemandBucketAdmin(ReadOnlyAdmin):
    list_display = ('sku', 'source', 'month', 'qty')
    list_filter = ('source',)
    search_fields = ('sku',)


@admin.register(models.SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = ('started_at', 'source', 'status', 'finished_at', 'triggered_by')
    list_filter = ('source', 'status')


@admin.register(models.ReminderSent)
class ReminderSentAdmin(ReadOnlyAdmin):
    list_display = ('sent_at', 'vendor_id', 'status', 'order_by_date', 'recipients')
