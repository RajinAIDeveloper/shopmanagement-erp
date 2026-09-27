"""Per-location stock, transfers, stock counts, multi-line purchase orders with partial receipts and supplier returns."""
import json
import uuid
from decimal import Decimal
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from .models import Product, Supplier, Location, StaffProfile, StockLevel, StockMovement, StockTransfer, StockCount, Purchase, PurchaseLine, JournalEntry, Order
from .services import create_order, transition_order, expire_due_reservations
from .inventory import default_location, add, take, available_at


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class InventoryTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner=User.objects.create_user('owner',password='test',is_staff=True,is_superuser=True)
        self.client.force_login(self.owner)
        self.main=default_location('pos')
        self.warehouse=Location.objects.create(name='Warehouse',code='WH',kind='warehouse')
        self.supplier=Supplier.objects.create(name='Supplier')
        self.monitor=Product.objects.create(name='Monitor',sku='MON',barcode='MON1',category='Displays',price=1000,cost=500,stock=0)
        self.cable=Product.objects.create(name='Cable',sku='CAB',barcode='CAB1',category='Accessories',price=200,cost=100,stock=0)
        add(self.monitor,self.main,20,'Opening stock','MON',self.owner)
        add(self.cable,self.main,50,'Opening stock','CAB',self.owner)

    def post(self,path,data,client=None):
        return (client or self.client).post(path,json.dumps(data),content_type='application/json')

    def inv(self,**data):
        return self.post('/api/inventory/action/',data)

    def level(self,product,location):
        return StockLevel.objects.filter(product=product,location=location).first()

    def staff(self,username,group):
        user=User.objects.create_user(username,password='test',is_staff=True)
        user.groups.add(Group.objects.get_or_create(name=group)[0])
        client=Client(); client.force_login(user)
        return client, user


class StockLevelTests(InventoryTestCase):
    def test_totals_follow_per_location_levels(self):
        self.monitor.refresh_from_db()
        self.assertEqual((self.monitor.stock,self.level(self.monitor,self.main).quantity),(20,20))
        add(self.monitor,self.warehouse,5,'Opening stock','MON',self.owner)
        self.monitor.refresh_from_db()
        self.assertEqual((self.monitor.stock,self.level(self.monitor,self.warehouse).quantity),(25,5))
        with self.assertRaises(ValueError): take(self.monitor,self.warehouse,6,'Adjustment','x',self.owner)
        movement=StockMovement.objects.filter(product=self.monitor,location=self.warehouse).latest('pk')
        self.assertEqual((movement.kind,movement.balance),('Opening stock',5))

    def test_legacy_products_materialise_their_level_at_the_default_location(self):
        legacy=Product.objects.create(name='Legacy',sku='LEG',barcode='LEG1',category='Misc',price=10,cost=5,stock=7,reserved=2)
        self.assertEqual(available_at(legacy,self.main),5)
        self.assertEqual(available_at(legacy,self.warehouse),5)
        take(legacy,self.main,3,'POS sale','x',self.owner)
        level=self.level(legacy,self.main)
        self.assertEqual((level.quantity,level.reserved),(4,2))
        self.assertEqual(available_at(legacy,self.warehouse),0)

    def test_pos_sales_come_from_the_chosen_location_and_pinned_staff_cannot_switch(self):
        add(self.cable,self.warehouse,3,'Opening stock','CAB',self.owner)
        order=create_order([{'product':self.cable.pk,'quantity':2}],'POS','Cash',staff=self.owner,location=self.warehouse.pk)
        self.assertEqual((order.location_id,self.level(self.cable,self.warehouse).quantity,self.level(self.cable,self.main).quantity),(self.warehouse.pk,1,50))
        with self.assertRaises(ValueError): create_order([{'product':self.cable.pk,'quantity':2}],'POS','Cash',staff=self.owner,location=self.warehouse.pk)
        client,cashier=self.staff('cashier','Cashier')
        StaffProfile.objects.create(user=cashier,location=self.main)
        response=self.post('/api/action/',{'action':'sale','items':[{'product':self.cable.pk,'quantity':1}],'payment':'Cash','location':self.warehouse.pk},client)
        self.assertEqual(response.json()['order']['location_id'],self.main.pk)
        self.assertEqual(self.client.get('/api/state/').json()['products'][0]['levels'][str(self.main.pk)]['quantity'],49)

    def test_online_orders_reserve_ship_cancel_and_expire_at_the_fulfilment_location(self):
        Location.objects.filter(pk=self.warehouse.pk).update(online_default=True)
        Location.objects.filter(pk=self.main.pk).update(online_default=False)
        add(self.monitor,self.warehouse,4,'Opening stock','MON',self.owner)
        self.assertEqual(Client().get('/api/catalog/').json()['products'][1]['available'],4)
        order=create_order([{'product':self.monitor.pk,'quantity':3}],'Online','COD')
        self.assertEqual((order.location_id,self.level(self.monitor,self.warehouse).reserved),(self.warehouse.pk,3))
        for status in ['Confirmed','Packed','Shipped']: transition_order(order.pk,status,self.owner)
        level=self.level(self.monitor,self.warehouse)
        self.assertEqual((level.quantity,level.reserved),(1,0))
        cancelled=create_order([{'product':self.monitor.pk,'quantity':1}],'Online','COD')
        transition_order(cancelled.pk,'Cancelled',self.owner)
        self.assertEqual(self.level(self.monitor,self.warehouse).reserved,0)
        with self.assertRaises(ValueError): create_order([{'product':self.monitor.pk,'quantity':2}],'Online','COD')
        self.assertEqual(self.level(self.monitor,self.main).quantity,20)

    def test_returns_and_adjustments_use_the_sale_location(self):
        add(self.cable,self.warehouse,3,'Opening stock','CAB',self.owner)
        order=create_order([{'product':self.cable.pk,'quantity':2}],'POS','Cash',staff=self.owner,location=self.warehouse.pk)
        transition_order(order.pk,'Returned',self.owner)
        self.assertEqual(self.level(self.cable,self.warehouse).quantity,3)
        self.assertEqual(self.post('/api/action/',{'action':'adjust','product':self.cable.pk,'quantity':-3,'reason':'Damaged','location':self.warehouse.pk}).status_code,200)
        self.assertEqual(self.post('/api/action/',{'action':'adjust','product':self.cable.pk,'quantity':-1,'reason':'Damaged','location':self.warehouse.pk}).status_code,400)
        self.assertEqual(self.level(self.cable,self.warehouse).quantity,0)


class PurchaseOrderTests(InventoryTestCase):
    def order(self,**extra):
        return self.post('/api/action/',{'action':'purchase','supplier':self.supplier.pk,'location':self.warehouse.pk,'lines':[{'product':self.monitor.pk,'quantity':10,'cost':'400'},{'product':self.cable.pk,'quantity':100,'cost':'80'}],**extra})

    def test_multi_line_order_with_partial_receipts_updates_cost_and_payable(self):
        response=self.order(expected_at='2026-10-01',note='October restock')
        self.assertEqual(response.status_code,200,response.content)
        purchase=Purchase.objects.get(pk=response.json()['id'])
        self.assertEqual((purchase.status,purchase.total,purchase.location_id),('Ordered',Decimal('12000'),self.warehouse.pk))
        monitor_line=purchase.lines.get(product=self.monitor)
        cable_line=purchase.lines.get(product=self.cable)
        self.assertEqual(self.post('/api/action/',{'action':'receive','id':purchase.pk,'lines':[{'line':monitor_line.pk,'quantity':11}]}).status_code,400)
        receipt=self.post('/api/action/',{'action':'receive','id':purchase.pk,'reference':'DN-1','lines':[{'line':monitor_line.pk,'quantity':4},{'line':cable_line.pk,'quantity':0}]})
        self.assertEqual(receipt.status_code,200,receipt.content)
        purchase.refresh_from_db(); self.monitor.refresh_from_db()
        self.assertEqual((purchase.status,purchase.received_value,purchase.payable),('Partially received',Decimal('1600'),Decimal('1600')))
        self.assertEqual((self.level(self.monitor,self.warehouse).quantity,self.monitor.stock,self.monitor.cost),(4,24,Decimal('483.33')))
        self.assertEqual(JournalEntry.objects.get(debit='Inventory',credit='Accounts payable').amount,Decimal('1600'))
        payables=self.client.get('/api/settlements/').json()['payables']
        self.assertEqual((payables[0]['number'],payables[0]['due']),(purchase.number,'1600.00'))
        self.assertEqual(self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'kind':'supplier_payment','target':purchase.pk,'amount':'1601','method':'Bank','reference':'x'}).status_code,400)
        self.assertEqual(self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'kind':'supplier_payment','target':purchase.pk,'amount':'1600','method':'Bank','reference':'x'}).status_code,200)
        self.assertEqual(self.post('/api/action/',{'action':'receive','id':purchase.pk}).status_code,200)
        purchase.refresh_from_db()
        self.assertEqual((purchase.status,purchase.received_value,purchase.receipts.count()),('Received',Decimal('12000'),2))
        self.assertEqual(self.post('/api/action/',{'action':'receive','id':purchase.pk}).status_code,400)
        state=self.client.get('/api/state/').json()['purchases'][0]
        self.assertEqual((state['status'],state['received_quantity'],state['due'],len(state['lines'])),('Received',110,10400.0,2))

    def test_supplier_returns_reduce_stock_and_payable(self):
        purchase=Purchase.objects.get(pk=self.order().json()['id'])
        self.post('/api/action/',{'action':'receive','id':purchase.pk})
        line=purchase.lines.get(product=self.cable)
        self.assertEqual(self.post('/api/action/',{'action':'purchase_return','id':purchase.pk,'reason':'Wrong colour','lines':[{'line':line.pk,'quantity':101}]}).status_code,400)
        response=self.post('/api/action/',{'action':'purchase_return','id':purchase.pk,'reason':'Wrong colour','lines':[{'line':line.pk,'quantity':30}]})
        self.assertEqual(response.status_code,200,response.content)
        purchase.refresh_from_db()
        self.assertEqual((purchase.returned_value,purchase.payable,self.level(self.cable,self.warehouse).quantity),(Decimal('2400'),Decimal('9600'),70))
        self.assertEqual(JournalEntry.objects.get(debit='Accounts payable',credit='Inventory').amount,Decimal('2400'))
        self.assertTrue(StockMovement.objects.filter(kind='Purchase return',quantity=-30).exists())
        self.assertEqual(self.client.get('/api/settlements/').json()['payables'][0]['due'],'9600.00')

    def test_orders_cannot_be_cancelled_after_receipt_and_need_valid_input(self):
        self.assertEqual(self.post('/api/action/',{'action':'purchase','supplier':self.supplier.pk,'lines':[]}).status_code,400)
        self.assertEqual(self.post('/api/action/',{'action':'purchase','supplier':999,'lines':[{'product':self.cable.pk,'quantity':1,'cost':'1'}]}).status_code,400)
        purchase=Purchase.objects.get(pk=self.order().json()['id'])
        self.assertEqual(self.post('/api/action/',{'action':'purchase_cancel','id':purchase.pk,'reason':'Changed supplier'}).status_code,200)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status,'Cancelled')
        self.assertEqual(self.post('/api/action/',{'action':'receive','id':purchase.pk}).status_code,400)
        other=Purchase.objects.get(pk=self.order().json()['id'])
        self.post('/api/action/',{'action':'receive','id':other.pk,'lines':[{'line':other.lines.first().pk,'quantity':1}]})
        self.assertEqual(self.post('/api/action/',{'action':'purchase_cancel','id':other.pk}).status_code,400)
        cashier,_=self.staff('cashier','Cashier')
        self.assertEqual(self.post('/api/action/',{'action':'purchase_return','id':other.pk,'lines':[]},cashier).status_code,403)


class TransferTests(InventoryTestCase):
    def test_transfer_moves_stock_in_stages_with_write_off(self):
        response=self.inv(action='transfer',source=self.main.pk,destination=self.warehouse.pk,note='Weekly top-up',lines=[{'product':self.monitor.pk,'quantity':5},{'product':self.cable.pk,'quantity':10}])
        self.assertEqual(response.status_code,200,response.content)
        transfer=StockTransfer.objects.get()
        self.assertEqual((transfer.status,self.level(self.monitor,self.main).quantity),('Draft',20))
        self.assertEqual(self.inv(action='dispatch',id=transfer.pk).status_code,200)
        self.assertEqual((self.level(self.monitor,self.main).quantity,self.level(self.monitor,self.warehouse)),(15,None))
        lines={l.product_id:l for l in transfer.lines.all()}
        self.assertEqual(self.inv(action='transfer_receive',id=transfer.pk,lines=[{'line':lines[self.monitor.pk].pk,'quantity':6}]).status_code,400)
        self.assertEqual(self.inv(action='transfer_receive',id=transfer.pk,lines=[{'line':lines[self.monitor.pk].pk,'quantity':4,'written_off':1},{'line':lines[self.cable.pk].pk,'quantity':10}]).status_code,200)
        transfer.refresh_from_db()
        self.assertEqual((transfer.status,self.level(self.monitor,self.warehouse).quantity,self.level(self.cable,self.warehouse).quantity),('Received',4,10))
        self.monitor.refresh_from_db()
        self.assertEqual(self.monitor.stock,19)
        self.assertEqual(JournalEntry.objects.get(debit='Inventory loss',credit='Inventory').amount,Decimal('500'))
        self.assertEqual(self.inv(action='transfer_cancel',id=transfer.pk).status_code,400)

    def test_cancelling_a_dispatched_transfer_returns_stock_to_the_source(self):
        transfer=StockTransfer.objects.get(pk=self.inv(action='transfer',source=self.main.pk,destination=self.warehouse.pk,dispatch=True,lines=[{'product':self.cable.pk,'quantity':10}]).json()['transfer']['id'])
        self.assertEqual((transfer.status,self.level(self.cable,self.main).quantity),('Dispatched',40))
        self.assertEqual(self.inv(action='transfer_cancel',id=transfer.pk,reason='Van broke down').status_code,200)
        self.assertEqual(self.level(self.cable,self.main).quantity,50)
        self.assertEqual(self.inv(action='transfer',source=self.main.pk,destination=self.main.pk,lines=[{'product':self.cable.pk,'quantity':1}]).status_code,400)
        self.assertEqual(self.inv(action='transfer',source=self.main.pk,destination=self.warehouse.pk,dispatch=True,lines=[{'product':self.cable.pk,'quantity':51}]).status_code,400)
        self.assertEqual(self.level(self.cable,self.main).quantity,50)


class StockCountTests(InventoryTestCase):
    def test_count_posts_variances_only_for_counted_lines(self):
        response=self.inv(action='count_start',location=self.main.pk)
        self.assertEqual(response.status_code,200,response.content)
        count=StockCount.objects.get()
        lines={l['product']:l for l in response.json()['count']['lines']}
        self.assertEqual((lines[self.monitor.pk]['expected'],lines[self.cable.pk]['expected']),(20,50))
        self.assertEqual(self.inv(action='count_start',location=self.main.pk).status_code,400)
        self.assertEqual(self.inv(action='count_record',id=count.pk,lines=[{'line':lines[self.monitor.pk]['id'],'counted':18}]).status_code,200)
        detail=self.client.get('/api/inventory/',{'count':count.pk}).json()['count']
        self.assertEqual((detail['lines_counted'],detail['lines_total']),(1,2))
        self.assertEqual(self.inv(action='count_complete',id=count.pk,lines=[{'line':lines[self.cable.pk]['id'],'counted':53}]).status_code,200)
        self.assertEqual((self.level(self.monitor,self.main).quantity,self.level(self.cable,self.main).quantity),(18,53))
        self.assertEqual(JournalEntry.objects.get(debit='Inventory loss').amount,Decimal('1000'))
        self.assertEqual(JournalEntry.objects.get(credit='Inventory gain').amount,Decimal('300'))
        self.assertEqual(StockMovement.objects.filter(kind='Stock count').count(),2)
        self.assertEqual(self.inv(action='count_complete',id=count.pk).status_code,400)

    def test_category_counts_and_cancellation(self):
        count_id=self.inv(action='count_start',location=self.warehouse.pk,category='Displays').json()['count']['id']
        count=StockCount.objects.get(pk=count_id)
        self.assertEqual([l.product_id for l in count.lines.all()],[self.monitor.pk])
        self.assertEqual(count.lines.get().expected,0)
        self.assertEqual(self.inv(action='count_cancel',id=count_id).status_code,200)
        self.assertEqual(self.inv(action='count_record',id=count_id,lines=[]).status_code,400)


class LocationAdminTests(InventoryTestCase):
    def test_locations_are_managed_by_managers_with_safe_defaults(self):
        stock_client,_=self.staff('stock','Inventory Manager')
        self.assertEqual(self.post('/api/inventory/action/',{'action':'location','name':'Branch','code':'br2'},stock_client).status_code,403)
        response=self.inv(action='location',name='Uttara branch',code='utt',kind='store',pos_default=True)
        self.assertEqual(response.status_code,200,response.content)
        branch=Location.objects.get(code='UTT')
        self.main.refresh_from_db()
        self.assertEqual((branch.pos_default,self.main.pos_default,self.main.online_default),(True,False,True))
        self.assertEqual(self.inv(action='location',name='Dup',code='UTT').status_code,400)
        self.assertEqual(self.inv(action='location',id=branch.pk,name='Uttara branch',code='UTT',active=False).status_code,400)
        self.assertEqual(self.inv(action='location',id=self.warehouse.pk,name='Warehouse',code='WH',active=False).status_code,200)
        add(self.cable,self.warehouse,1,'Opening stock','x',self.owner)
        Location.objects.filter(pk=self.warehouse.pk).update(active=True)
        self.assertEqual(self.inv(action='location',id=self.warehouse.pk,name='Warehouse',code='WH',active=False).status_code,400)
        overview=self.client.get('/api/inventory/').json()
        self.assertEqual(len(overview['locations']),3)
        self.assertEqual(self.inv(action='staff_location',user=self.owner.pk,location=branch.pk).status_code,200)
        self.assertEqual(self.client.get('/api/state/').json()['pos_location'],branch.pk)
        self.assertEqual([s['location'] for s in overview['staff'] if s['id']==self.owner.pk],[None])
