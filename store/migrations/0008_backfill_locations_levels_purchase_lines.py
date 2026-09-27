from django.db import migrations


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Location = apps.get_model('store', 'Location')
    Product = apps.get_model('store', 'Product')
    StockLevel = apps.get_model('store', 'StockLevel')
    Order = apps.get_model('store', 'Order')
    StockMovement = apps.get_model('store', 'StockMovement')
    Purchase = apps.get_model('store', 'Purchase')
    PurchaseLine = apps.get_model('store', 'PurchaseLine')
    AuditLog = apps.get_model('store', 'AuditLog')
    main = Location.objects.using(alias).filter(code='MAIN').first() or Location.objects.using(alias).create(name='Main store', code='MAIN', kind='store', pos_default=True, online_default=True)
    levels = 0
    for p in Product.objects.using(alias).iterator():
        if not StockLevel.objects.using(alias).filter(product=p).exists():
            StockLevel.objects.using(alias).create(product=p, location=main, quantity=p.stock, reserved=p.reserved)
            levels += 1
    Order.objects.using(alias).filter(location__isnull=True).update(location=main)
    StockMovement.objects.using(alias).filter(location__isnull=True).update(location=main)
    converted = 0
    for purchase in Purchase.objects.using(alias).iterator():
        if purchase.product_id and not PurchaseLine.objects.using(alias).filter(purchase=purchase).exists():
            PurchaseLine.objects.using(alias).create(purchase=purchase, product_id=purchase.product_id, quantity=purchase.quantity, received_quantity=purchase.quantity if purchase.received else 0, cost=purchase.cost)
            converted += 1
        Purchase.objects.using(alias).filter(pk=purchase.pk).update(location=main, status='Received' if purchase.received else 'Ordered')
    AuditLog.objects.using(alias).create(action='Locations enabled', detail=f'All stock assigned to {main.name}: {levels} products, {converted} purchase orders converted to line items.')


class Migration(migrations.Migration):
    dependencies = [('store', '0007_locations_transfers_counts_purchase_lines')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
