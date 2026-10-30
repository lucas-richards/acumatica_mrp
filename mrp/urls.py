from django.urls import path

from . import views

app_name = 'mrp'

urlpatterns = [
    path('', views.plan, name='plan'),
    path('vendors/search/', views.vendor_search, name='vendor_search'),
    # SKU is last and uses <path:> because Acumatica inventory IDs may contain "/"
    path('breakdown/<path:sku>', views.breakdown, name='breakdown'),
    path('fg-breakdown/<path:sku>', views.fg_breakdown, name='fg_breakdown'),
    path('export.csv', views.export_csv, name='export_csv'),
    path('refresh/', views.refresh, name='refresh'),
    path('refresh/status/', views.refresh_status, name='refresh_status'),
    path('lead-times/', views.lead_times, name='lead_times'),
    path('lead-times/delete/<path:sku>', views.delete_lead_time, name='delete_lead_time'),
]
