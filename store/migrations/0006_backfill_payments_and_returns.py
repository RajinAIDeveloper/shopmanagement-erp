from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Order = apps.get_model('store', 'Order')
    OrderLine = apps.get_model('store', 'OrderLine')
    OrderPayment = apps.get_model('store', 'OrderPayment')
    AuditLog = apps.get_model('store', 'AuditLog')
    # Historical POS sales were single-tender; give them a payment row so shift and receipt views stay complete.
    created = 0
    for o in Order.objects.using(alias).filter(channel='POS', paid_total__gt=0).iterator():
        if not OrderPayment.objects.using(alias).filter(order=o).exists():
            OrderPayment.objects.using(alias).create(order=o, method=o.payment if o.payment in ['Cash', 'Card', 'bKash', 'Nagad', 'Bank'] else 'Cash', amount=o.paid_total, reference='')
            created += 1
    # Fully returned orders now carry the returned value on the order and its lines.
    returned = Order.objects.using(alias).filter(status='Returned', returned_total=0)
    count = returned.count()
    for o in returned.iterator():
        OrderLine.objects.using(alias).filter(order=o).update(returned_quantity=F('quantity'))
        Order.objects.using(alias).filter(pk=o.pk).update(returned_total=F('total'))
    AuditLog.objects.using(alias).create(action='Tenders, VAT and partial returns enabled', detail=f'{created} historical sales received payment rows; {count} returned sales were marked fully returned. VAT is recorded on sales from this point on.')


class Migration(migrations.Migration):
    dependencies = [('store', '0005_selling_vat_tenders_returns_shifts')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
