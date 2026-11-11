from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('mrp', '0008_internal_demand_sources'),
    ]

    operations = [
        migrations.RemoveField(model_name='itemvendor', name='last_cost'),
        migrations.RemoveField(model_name='pohistory', name='unit_cost'),
    ]
