from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from planning.demand import DEMAND_SOURCES, INTERNAL_SOURCES
from planning.mrp import MAX_COVER_MONTHS
from planning.models import BASIS_CUSTOM, BASIS_SIX, BASIS_TWO_YEAR, QtyMode
from planning.reminders import REMINDER_DAY_CHOICES

QTY = dict(max_digits=14, decimal_places=4)


# ---------------------------------------------------------------------------
# Data synced from Acumatica (full refresh). Items are referenced by SKU rather
# than foreign keys so a partial or inconsistent ERP extract never fails to load.
# ---------------------------------------------------------------------------

class Vendor(models.Model):
    vendor_id = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=255)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    email = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class StockItem(models.Model):
    COMPONENT = 'component'
    FINISHED_GOOD = 'finished_good'
    KIND_CHOICES = [(COMPONENT, 'Component'), (FINISHED_GOOD, 'Finished good')]

    sku = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=255, blank=True)
    item_class = models.CharField(max_length=60, blank=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    uom = models.CharField(max_length=20, blank=True)
    lot_size = models.DecimalField(**QTY, null=True, blank=True)
    # Available quantity across warehouses (Acumatica's "Available"); null when not synced
    qty_available = models.DecimalField(**QTY, null=True, blank=True)
    qty_receiving = models.DecimalField(**QTY, null=True, blank=True)   # in the receiving location, not in available
    item_type = models.CharField(max_length=30, blank=True)            # Finished Good, Subassembly, Component Part
    # Inventory Planning tab "Source": Manufacturing, Purchase, Mixed (classes disagree) or blank
    replenishment_source = models.CharField(max_length=20, blank=True)
    planning_method = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ['sku']

    def __str__(self):
        return f'{self.sku} · {self.description}'


class OnHand(models.Model):
    sku = models.CharField(max_length=60, db_index=True)
    warehouse = models.CharField(max_length=30)
    qty = models.DecimalField(**QTY)

    class Meta:
        verbose_name_plural = 'on hand'


class OpenPOLine(models.Model):
    order_nbr = models.CharField(max_length=30)
    line_nbr = models.PositiveIntegerField()
    sku = models.CharField(max_length=60, db_index=True)
    vendor_id = models.CharField(max_length=30)
    qty_open = models.DecimalField(**QTY)
    promised_date = models.DateField(null=True, blank=True)
    order_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'open PO line'


class BomLine(models.Model):
    fg_sku = models.CharField(max_length=60, db_index=True)
    component_sku = models.CharField(max_length=60, db_index=True)
    qty_per = models.DecimalField(**QTY)

    class Meta:
        verbose_name = 'BOM line'


class ItemVendor(models.Model):
    sku = models.CharField(max_length=60, db_index=True)
    vendor_id = models.CharField(max_length=30)
    is_default = models.BooleanField(default=False)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    lot_size = models.DecimalField(**QTY, null=True, blank=True)


class PoHistory(models.Model):
    sku = models.CharField(max_length=60, db_index=True)
    vendor_id = models.CharField(max_length=30)
    order_nbr = models.CharField(max_length=30)
    order_date = models.DateField()
    qty = models.DecimalField(**QTY)

    class Meta:
        verbose_name_plural = 'PO history'


class LastProduction(models.Model):
    """The most recent completed production run of a finished good."""
    sku = models.CharField(max_length=60, db_index=True)
    qty = models.DecimalField(**QTY)
    run_date = models.DateField()


class DemandBucket(models.Model):
    """Quantity per month for one demand source: finished-good demand, or an internal source (issues, assembly, open orders)."""
    FG_SOURCE_CHOICES = [(name, name.replace('_', ' ').capitalize()) for name in DEMAND_SOURCES]
    SOURCE_CHOICES = FG_SOURCE_CHOICES + [(name, name.replace('_', ' ').capitalize()) for name in INTERNAL_SOURCES]

    sku = models.CharField(max_length=60, db_index=True)
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES)
    month = models.DateField(help_text='First day of the month')
    qty = models.DecimalField(**QTY)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['sku', 'source', 'month'], name='uniq_demand_bucket')]


class SyncRun(models.Model):
    ACUMATICA = 'acumatica'
    IMPORT = 'import'
    SAMPLE = 'sample'
    SOURCE_CHOICES = [(ACUMATICA, 'Acumatica'), (IMPORT, 'File import'), (SAMPLE, 'Sample data')]
    RUNNING, OK, FAILED = 'running', 'ok', 'failed'
    STATUS_CHOICES = [(RUNNING, 'Running'), (OK, 'OK'), (FAILED, 'Failed')]

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    pid = models.PositiveIntegerField(null=True, blank=True, help_text='Process running the sync')

    class Meta:
        ordering = ['-started_at']
        get_latest_by = 'started_at'


# ---------------------------------------------------------------------------
# Planner settings
# ---------------------------------------------------------------------------

class PlanSettings(models.Model):
    QTY_MODE_CHOICES = [(QtyMode.COVER_TARGET.value, 'To reach cover target'),
                        (QtyMode.MATCH_LAST_PO.value, 'Match last PO')]
    DEMAND_CHOICES = DemandBucket.FG_SOURCE_CHOICES

    cover_months = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(0), MaxValueValidator(MAX_COVER_MONTHS)])
    qty_mode = models.CharField(max_length=10, choices=QTY_MODE_CHOICES, default=QtyMode.COVER_TARGET.value)
    reminder_days = models.PositiveSmallIntegerField(default=30, choices=[(d, f'{d} days') for d in REMINDER_DAY_CHOICES])
    demand_source = models.CharField(max_length=30, choices=DEMAND_CHOICES, default='trailing_invoices')
    demand_window_months = models.PositiveSmallIntegerField(
        default=6, validators=[MinValueValidator(1), MaxValueValidator(24)])
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'plan settings'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class UsageChoice(models.Model):
    """Which monthly usage drives one item's plan, and the custom usage typed in for it. Items without a
    row use the recent (6 month) usage."""
    BASIS_CHOICES = [(BASIS_SIX, 'Recent months'), (BASIS_TWO_YEAR, 'Two year average'), (BASIS_CUSTOM, 'Custom')]

    sku = models.CharField(max_length=60, unique=True)
    basis = models.CharField(max_length=10, choices=BASIS_CHOICES, default=BASIS_SIX)
    custom_usage = models.DecimalField(**QTY, null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)


class WarningAck(models.Model):
    """Someone marked a vendor warning on an item as done. It stays hidden for a week and comes back if the
    problem is still there."""
    sku = models.CharField(max_length=60)
    flag = models.CharField(max_length=30)
    acked_on = models.DateField()
    acked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['sku', 'flag'], name='uniq_warning_ack')]


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

class ReminderSent(models.Model):
    vendor_id = models.CharField(max_length=30, db_index=True)
    status = models.CharField(max_length=30)
    order_by_date = models.DateField(null=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    recipients = models.TextField()

    class Meta:
        ordering = ['-sent_at']


class WeeklyDigestSubscription(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mrp_digest')
    created_at = models.DateTimeField(auto_now_add=True)
