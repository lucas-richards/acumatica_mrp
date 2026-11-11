from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mrp', '0009_remove_costs'),
    ]

    operations = [
        migrations.CreateModel(
            name='LastProduction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sku', models.CharField(db_index=True, max_length=60)),
                ('qty', models.DecimalField(decimal_places=4, max_digits=14)),
                ('run_date', models.DateField()),
            ],
        ),
    ]
