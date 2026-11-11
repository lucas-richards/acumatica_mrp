from django.urls import path

from . import views

app_name = 'mrp'

urlpatterns = [
    path('', views.plan, name='plan'),
    # SKU is last and uses <path:> because Acumatica inventory IDs may contain "/"
    path('breakdown/<path:sku>', views.breakdown, name='breakdown'),
    path('report/<path:sku>', views.report, name='report'),
    path('usage/', views.set_usage, name='set_usage'),
    path('warning-done/', views.warning_done, name='warning_done'),
    path('export.csv', views.export_csv, name='export_csv'),
    path('refresh/', views.refresh, name='refresh'),
    path('refresh/status/', views.refresh_status, name='refresh_status'),
]
