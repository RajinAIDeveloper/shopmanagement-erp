import random
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
import uuid
from store.models import Product, Supplier, Customer, Order, Purchase, Expense, JournalEntry, StockMovement, AuditLog, CategoryAttribute, Offer, Announcement, Review
from store.services import create_order, transition_order, movement, journal
from store.operations import record_settlement
from store.inventory import add, default_location, create_purchase, create_transfer, receive_transfer
from store.models import Location


class Command(BaseCommand):
    help = 'Populate an empty development database with sample Altab Tech data.'

    def add_arguments(self, parser):
        parser.add_argument('--password', required=True, help='Password for the demo owner account')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG: raise CommandError('Demo data is only available in DEBUG mode.')
        for role in ['Owner','Manager','Cashier','Inventory Manager','Accountant']:
            Group.objects.get_or_create(name=role)
        if Product.objects.exists():
            self.stdout.write('Products already exist. No sample data added or credentials changed.')
            return
        user, created = User.objects.get_or_create(username='owner',defaults={'first_name':'Altab','is_staff':True,'is_superuser':True})
        if created:
            user.set_password(options['password'])
            user.save()
        main=default_location('pos')
        warehouse=Location.objects.create(name='Warehouse',code='WH',kind='warehouse',address='Tejgaon, Dhaka')
        suppliers = [Supplier.objects.create(name=name,email=f'orders{i}@example.com',phone=f'0171100000{i}') for i,name in enumerate(['Tech Distribution BD','Gadget Source','Digital Connections'])]
        entries = [
            ('Sony WH-1000XM5','Audio','Sony','Midnight black',32900,25800,18,'headphones'),
            ('AirPods Pro (2nd gen)','Audio','Apple','USB-C · White',28500,22400,24,'earbuds'),
            ('Logitech MX Master 3S','Peripherals','Logitech','Graphite',11500,8100,28,'mouse'),
            ('Keychron K2 Wireless','Peripherals','Keychron','Brown switch · 75%',8900,6100,19,'keyboard'),
            ('Apple Watch SE','Wearables','Apple','44mm · Starlight',29900,23800,17,'watch'),
            ('JBL Flip 6','Audio','JBL','Portable · Black',11900,8200,27,'speaker'),
            ('Anker Nano 30W','Accessories','Anker','USB-C · White',2190,1350,8,'charger'),
            ('UGREEN USB-C Hub','Accessories','UGREEN','6-in-1 · Space gray',3490,2100,7,'cable'),
            ('Samsung 24” Monitor','Displays','Samsung','FHD · 75Hz',16900,12800,20,'monitor'),
            ('Baseus USB-C Cable','Accessories','Baseus','100W · 2 meters',790,320,9,'cable'),
            ('Logitech Pebble M350','Peripherals','Logitech','Silent · Off-white',2490,1650,35,'mouse'),
            ('Amazfit GTS 4 Mini','Wearables','Amazfit','Midnight black',10900,7600,22,'watch'),
        ]
        products=[]
        for i,(name,category,brand,variant,price,cost,stock,image) in enumerate(entries):
            p=Product.objects.create(name=name,category=category,brand=brand,variant=variant,price=price,cost=cost,stock=0,sku=f'AT-{i+1:04d}',barcode=f'1000000{i+1:05d}',image=image,supplier=suppliers[i%3])
            add(p,main,stock+80,'Opening stock','Demo opening balance',user)
            journal(p.sku,'Inventory','Opening equity',p.stock*p.cost)
            products.append(p)
        names=['Nusrat Jahan','Arif Rahman','Tanvir Ahmed','Farhana Islam','Mehedi Hasan','Sadia Afrin','Rafiul Karim','Nabila Chowdhury','Imran Hossain','Samiha Khan','Fahim Islam','Ayesha Sultana']
        customers=[Customer.objects.create(name=name,email=name.lower().replace(' ','.')+'@example.com',phone=f'01712345{i:03d}',address='Dhaka, Bangladesh') for i,name in enumerate(names)]
        rng=random.Random(42)
        for days in range(29,-1,-1):
            for j in range(rng.randint(2,5)):
                p=rng.choice(products[:6]+products[:4])
                channel='POS' if rng.random()<.7 else 'Online'
                o=create_order([{'product':p.pk,'quantity':rng.randint(1,2)}],channel,'Cash' if channel=='POS' else 'COD',rng.choice(customers),staff=user if channel=='POS' else None)
                if channel=='Online':
                    target='Pending' if days==0 else ('Packed' if days<2 else 'Delivered')
                    for status in ['Confirmed','Packed','Shipped','Delivered']:
                        if target=='Pending':break
                        transition_order(o.pk,status,user)
                        if status==target:break
                created_at=timezone.now()-timedelta(days=days,hours=rng.randint(0,5),minutes=rng.randint(0,59))
                Order.objects.filter(pk=o.pk).update(created_at=created_at)
                JournalEntry.objects.filter(reference=o.number).update(created_at=created_at)
                StockMovement.objects.filter(reference=o.number).update(created_at=created_at)
        from store.inventory import take
        for p,entry in zip(products,entries):
            p.refresh_from_db()
            target=entry[6]+p.reserved
            delta=target-p.stock
            if delta>0: add(p,main,delta,'Adjustment','Demo physical stock count',user)
            elif delta<0: take(p,main,-delta,'Adjustment','Demo physical stock count',user)
            journal(p.sku,'Inventory' if delta>0 else 'Inventory loss','Inventory gain' if delta>0 else 'Inventory',abs(delta)*p.cost)
        create_purchase({'supplier':suppliers[0].pk,'location':main.pk,'note':'Restock of accessories','lines':[{'product':p.pk,'quantity':25,'cost':str(p.cost)} for p in products[6:9]]},user)
        create_purchase({'supplier':suppliers[1].pk,'location':warehouse.pk,'lines':[{'product':products[0].pk,'quantity':10,'cost':str(products[0].cost)}]},user)
        transfer=create_transfer({'source':main.pk,'destination':warehouse.pk,'note':'Overflow stock to warehouse','lines':[{'product':products[2].pk,'quantity':5},{'product':products[3].pk,'quantity':4}],'dispatch':True},user)
        receive_transfer({'id':transfer.pk},user)
        for description,category,amount in [('September store rent','Rent',18000),('Internet connection','Utilities',1500),('Packaging supplies','Other',2400)]:
            expense=Expense.objects.create(description=description,category=category,amount=amount)
            journal(f'Expense #{expense.pk}','Operating expenses','Cash',Decimal(amount))
        self.seed_commerce(products,customers,user,rng)
        AuditLog.objects.create(staff=user,action='Demo workspace initialized',detail='Sample products, customers, sales, offers, reviews, payments and expenses. Not real business records.')
        self.stdout.write(self.style.SUCCESS('Altab Tech demo ready. Username: owner. Use the password you supplied.'))

    def seed_commerce(self,products,customers,user,rng):
        """Storefront merchandising, category specifications, guest reviews, and a few recorded COD collections."""
        by_name={p.name:p for p in products}
        specs={
            'Audio':[('Connection type',['Bluetooth','USB-C','3.5 mm']),('Noise cancelling',['Active','Passive','None'])],
            'Peripherals':[('Connection type',['Bluetooth','USB-C','2.4 GHz receiver']),('Colour',['Black','Graphite','Off-white'])],
            'Displays':[('Refresh rate',['60 Hz','75 Hz','144 Hz']),('Panel',['IPS','VA'])],
            'Wearables':[('Case size',['40 mm','44 mm']),('Water resistance',['5 ATM','IP68'])],
        }
        attributes={}
        for category,definitions in specs.items():
            for position,(name,options) in enumerate(definitions):
                attributes[(category,name)]=CategoryAttribute.objects.create(category=category,name=name,options=options,position=position)
        assignments={
            'Sony WH-1000XM5':{('Audio','Connection type'):['Bluetooth','3.5 mm'],('Audio','Noise cancelling'):['Active']},
            'AirPods Pro (2nd gen)':{('Audio','Connection type'):['Bluetooth','USB-C'],('Audio','Noise cancelling'):['Active']},
            'JBL Flip 6':{('Audio','Connection type'):['Bluetooth'],('Audio','Noise cancelling'):['None']},
            'Logitech MX Master 3S':{('Peripherals','Connection type'):['Bluetooth','2.4 GHz receiver'],('Peripherals','Colour'):['Graphite']},
            'Keychron K2 Wireless':{('Peripherals','Connection type'):['Bluetooth','USB-C'],('Peripherals','Colour'):['Black']},
            'Logitech Pebble M350':{('Peripherals','Connection type'):['Bluetooth','2.4 GHz receiver'],('Peripherals','Colour'):['Off-white']},
            'Samsung 24” Monitor':{('Displays','Refresh rate'):['75 Hz'],('Displays','Panel'):['IPS']},
            'Apple Watch SE':{('Wearables','Case size'):['44 mm'],('Wearables','Water resistance'):['5 ATM']},
            'Amazfit GTS 4 Mini':{('Wearables','Case size'):['40 mm'],('Wearables','Water resistance'):['5 ATM']},
        }
        for name,values in assignments.items():
            p=by_name[name]
            p.attributes={str(attributes[key].pk):choices for key,choices in values.items()}
            p.save(update_fields=['attributes'])
        Offer.objects.create(title='Audio week',description='10% off every pair of headphones, earbuds and speakers this week.',category='Audio',discount_type='percent',value=10,ends_at=timezone.now()+timedelta(days=7))
        Offer.objects.create(title='Monitor upgrade deal',description='Take ৳1,500 off the Samsung 24” monitor while stock lasts.',product=by_name['Samsung 24” Monitor'],discount_type='fixed',value=1500,ends_at=timezone.now()+timedelta(days=14))
        Announcement.objects.create(message='Free delivery inside Dhaka on orders over ৳5,000.',link='/shop/#collection',position=1)
        Announcement.objects.create(message='Audio week: 10% off headphones, earbuds and speakers.',link='/shop/#offers',position=2)
        reviews=[
            ('Sony WH-1000XM5','Tanvir A.',5,'Silence, finally','Noise cancelling is superb on the Dhaka commute and the battery lasts all week.','approved'),
            ('Sony WH-1000XM5','Sadia',4,'Great sound, snug fit','Sound is rich and warm. Slightly tight over glasses, but comfortable after a break-in.','approved'),
            ('Logitech MX Master 3S','Arif R.',5,'The best work mouse','The scroll wheel alone is worth it. Pairs with three devices without fuss.','approved'),
            ('Keychron K2 Wireless','Mehedi',4,'Lovely typing feel','Brown switches are quiet enough for the office. Wish the battery indicator were clearer.','approved'),
            ('JBL Flip 6','Nusrat',5,'Party in a bottle','Loud, punchy and survived a rooftop rain shower.','pending'),
        ]
        for name,author,rating,title,body,status in reviews:
            Review.objects.create(product=by_name[name],author=author,rating=rating,title=title,body=body,visitor=uuid.uuid4(),status=status)
        delivered=list(Order.objects.filter(channel='Online',status='Delivered').order_by('created_at'))
        for order in delivered[:max(1,len(delivered)-3)]:
            record_settlement({'request_id':str(uuid.uuid4()),'kind':'collection','target':order.pk,'amount':str(order.total),'method':rng.choice(['Cash','bKash','Nagad']),'reference':f'Courier COD remittance {order.number}','note':'Demo settlement'},user)
