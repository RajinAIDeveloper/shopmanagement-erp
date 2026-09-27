from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('store', '0008_backfill_locations_levels_purchase_lines')]
    operations = [
        migrations.RemoveField(model_name='purchase', name='cost'),
        migrations.RemoveField(model_name='purchase', name='product'),
        migrations.RemoveField(model_name='purchase', name='quantity'),
        migrations.RemoveField(model_name='purchase', name='received'),
    ]
