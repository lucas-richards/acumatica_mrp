from django.urls import path

from . import views

app_name = 'mrp'

urlpatterns = [
    path('', views.plan, name='plan'),
    # SKU is last and uses <path:> because Acumatica inventory IDs may contain "/"
    path('breakdown/<path:sku>', views.breakdown, name='breakdown'),
    path('export.csv', views.export_csv, name='export_csv'),
    path('order-by-dates.ics', views.calendar_ics, name='calendar_ics'),
    path('settings/defaults/', views.save_defaults, name='save_defaults'),
    path('settings/reminders/', views.save_reminder_days, name='save_reminder_days'),
    path('digest/', views.toggle_digest, name='toggle_digest'),
    path('refresh/', views.refresh, name='refresh'),
    path('lead-times/', views.lead_times, name='lead_times'),
    path('lead-times/delete/<path:sku>', views.delete_lead_time, name='delete_lead_time'),
]
