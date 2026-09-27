from datetime import timedelta
from django.db import migrations
from django.db.models import F
from django.utils import timezone


def backfill(apps,schema_editor):
    alias=schema_editor.connection.alias
    Order=apps.get_model('store','Order')
    OrderLine=apps.get_model('store','OrderLine')
    AuditLog=apps.get_model('store','AuditLog')
    Order.objects.using(alias).filter(channel='POS').update(paid_total=F('total'))
    # Older POS returns already credited cash directly. Do not create a second refund.
    Order.objects.using(alias).filter(channel='POS',status='Returned').update(refunded_total=F('total'))
    for line in OrderLine.objects.using(alias).select_related('product').iterator():
        line.name=line.product.name
        line.sku=line.product.sku
        line.variant=line.product.variant
        line.save(using=alias,update_fields=['name','sku','variant'])
    count=Order.objects.using(alias).filter(channel='Online',status='Pending').update(reservation_expires_at=timezone.now()+timedelta(hours=24))
    AuditLog.objects.using(alias).create(action='Payment tracking and reservation expiry enabled',detail=f'Historical POS payments and returns preserved. {count} existing unconfirmed online orders received a 24-hour grace period. Order-line descriptions were snapshotted.')


class Migration(migrations.Migration):
    dependencies=[('store','0003_settlement_order_paid_total_order_refunded_total_and_more')]
    operations=[migrations.RunPython(backfill,migrations.RunPython.noop)]
