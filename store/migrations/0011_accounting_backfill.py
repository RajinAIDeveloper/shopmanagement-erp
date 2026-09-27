from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    Account = apps.get_model('store', 'Account')
    JournalEntry = apps.get_model('store', 'JournalEntry')
    # Frozen chart for the legacy operational accounts.
    chart = [
        ('1000','Cash','asset',True), ('1010','Bank / mobile payments','asset',True),
        ('1100','Accounts receivable','asset',False), ('1200','Inventory','asset',False),
        ('2000','Accounts payable','liability',False), ('2100','VAT payable','liability',False),
        ('2200','Customer refunds payable','liability',False), ('2300','Store credit liability','liability',False),
        ('2400','Customer advances','liability',False), ('3000','Opening equity','equity',False),
        ('4000','Sales revenue','income',False), ('4100','Inventory gain','income',False),
        ('4200','Shipping revenue','income',False), ('5000','Cost of goods sold','expense',False),
        ('5100','Operating expenses','expense',False), ('5200','Inventory loss','expense',False),
        ('5300','Cash over / short','expense',False), ('5400','Customer goodwill','expense',False),
        ('5500','Courier fees','expense',False),
    ]
    for code, name, kind, cash in chart:
        Account.objects.get_or_create(name=name, defaults={'code':code,'kind':kind,'cash':cash,'system':True,'cash_flow':'financing' if kind=='equity' else 'operating'})
    for entry in JournalEntry.objects.iterator():
        JournalEntry.objects.filter(pk=entry.pk).update(effective_date=timezone.localdate(entry.created_at))


class Migration(migrations.Migration):
    dependencies = [('store', '0010_accounting')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
