"""Product maintenance, settlements, audit access, reservation expiry, and production plumbing."""
import io
import json
import sqlite3
from contextlib import closing
import tempfile
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.utils import timezone
from .models import Product, Order, OrderLine, Purchase, Supplier, Settlement, JournalEntry, AuditLog, StockMovement, StoreProfile, CategoryAttribute
from .services import create_order, transition_order, expire_due_reservations


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class OperationsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner=User.objects.create_user('owner',password='test',is_staff=True,is_superuser=True)
        self.client.force_login(self.owner)
        self.supplier=Supplier.objects.create(name='Supplier')
        self.product=Product.objects.create(name='Monitor',sku='MON',barcode='MON123',category='Displays',price=1000,cost=500,stock=30,supplier=self.supplier)

    def post(self,path,data,client=None):
        return (client or self.client).post(path,json.dumps(data),content_type='application/json')

    def staff(self,username,group):
        user=User.objects.create_user(username,password='test',is_staff=True)
        user.groups.add(Group.objects.get_or_create(name=group)[0])
        client=Client()
        client.force_login(user)
        return client

    def shipped_order(self,quantity=2):
        order=create_order([{'product':self.product.pk,'quantity':quantity}],'Online','COD')
        for status in ['Confirmed','Packed','Shipped']: transition_order(order.pk,status,self.owner)
        return order

    def settle(self,client=None,**data):
        return self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'method':'Cash','reference':'Receipt 1',**data},client)


class ProductMaintenanceTests(OperationsTestCase):
    def edit(self,**changes):
        base={'id':self.product.pk,'name':'Monitor','sku':'MON','barcode':'MON123','category':'Displays','brand':'','variant':'','price':'1000','reorder_level':10,'active':True,'supplier':self.supplier.pk}
        return self.post('/api/products/save/',{**base,**changes})

    def test_edit_updates_catalog_fields_and_records_before_after_diff(self):
        response=self.edit(name='Monitor 24"',price='1200.50',variant='75 Hz',brand='Samsung',supplier='')
        self.assertEqual(response.status_code,200,response.content)
        self.product.refresh_from_db()
        self.assertEqual((self.product.name,self.product.price,self.product.variant,self.product.supplier),('Monitor 24"',Decimal('1200.50'),'75 Hz',None))
        detail=json.loads(AuditLog.objects.filter(action__startswith='Product updated').latest('pk').detail)
        self.assertEqual((detail['before']['name'],detail['after']['name'],detail['after']['price']),('Monitor','Monitor 24"','1200.50'))

    def test_stock_and_cost_cannot_be_overwritten_through_editing(self):
        self.assertEqual(self.edit(stock=99).status_code,400)
        self.assertEqual(self.edit(cost='1').status_code,400)
        self.assertEqual(self.edit(stock=30,cost='500').status_code,200)
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock,self.product.cost),(30,Decimal('500')))

    def test_category_change_requires_clearing_specifications(self):
        attribute=CategoryAttribute.objects.create(category='Displays',name='Panel',options=['IPS'])
        Product.objects.filter(pk=self.product.pk).update(attributes={str(attribute.pk):['IPS']})
        self.assertEqual(self.edit(category='Laptops').status_code,400)
        self.assertEqual(self.edit(category='Laptops',clear_attributes=True).status_code,200)
        self.product.refresh_from_db()
        self.assertEqual((self.product.category,self.product.attributes),('Laptops',{}))

    def test_duplicate_sku_or_barcode_is_rejected_cleanly(self):
        Product.objects.create(name='Other',sku='OTHER',barcode='OTHER1',category='Displays',price=1,cost=1)
        self.assertEqual(self.edit(sku='OTHER').status_code,400)
        self.assertEqual(self.edit(barcode='OTHER1').status_code,400)

    def test_photo_must_come_from_the_upload_endpoint(self):
        self.assertEqual(self.edit(photo='https://evil.test/x.png').status_code,400)
        self.assertEqual(self.edit(photo='/media/uploads/'+'a'*32+'.png').status_code,400)

    def test_deactivated_products_leave_public_catalog_and_cannot_be_sold(self):
        self.assertEqual(self.edit(active=False).status_code,200)
        self.assertEqual(Client().get('/api/catalog/').json()['products'],[])
        staff_product=self.client.get('/api/state/').json()['products'][0]
        self.assertFalse(staff_product['active'])
        with self.assertRaises(ValueError): create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash')

    def test_only_inventory_roles_can_edit_products(self):
        self.assertEqual(self.edit(name='Cashier edit').status_code,200)
        cashier=self.staff('cashier','Cashier')
        self.assertEqual(self.post('/api/products/save/',{'id':self.product.pk,'name':'Nope','sku':'MON','barcode':'MON123','category':'Displays','price':'1'},cashier).status_code,403)
        inventory=self.staff('stock','Inventory Manager')
        self.assertEqual(self.post('/api/products/save/',{'id':self.product.pk,'name':'Stock edit','sku':'MON','barcode':'MON123','category':'Displays','price':'1'},inventory).status_code,200)

    def test_order_lines_keep_their_description_after_product_edits(self):
        order=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        self.assertEqual(self.edit(name='Renamed monitor',sku='MON-2').status_code,200)
        line=OrderLine.objects.get(order=order)
        self.assertEqual((line.name,line.sku),('Monitor','MON'))
        self.assertContains(self.client.get(f'/receipt/{order.pk}/'),'Monitor')
        self.assertNotContains(self.client.get(f'/receipt/{order.pk}/'),'Renamed monitor')
        self.assertEqual(self.client.get('/api/state/').json()['orders'][0]['items'][0]['name'],'Monitor')


class SettlementTests(OperationsTestCase):
    def test_pos_sales_are_paid_in_full_at_the_counter(self):
        order=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        self.assertEqual((order.paid_total,order.refunded_total),(order.total,0))

    def test_collection_is_idempotent_partial_and_bounded(self):
        order=self.shipped_order()
        balances=self.client.get('/api/settlements/').json()
        self.assertEqual(balances['receivables'][0]['due'],'2000.00')
        request_id=str(uuid.uuid4())
        first=self.settle(kind='collection',target=order.pk,amount='500',request_id=request_id)
        self.assertEqual(first.status_code,200,first.content)
        replay=self.settle(kind='collection',target=order.pk,amount='500',request_id=request_id)
        self.assertEqual(replay.json()['id'],first.json()['id'])
        self.assertEqual(Settlement.objects.count(),1)
        self.assertEqual(JournalEntry.objects.filter(debit='Cash',credit='Accounts receivable').count(),1)
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='600',request_id=request_id).status_code,400)
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='1600').status_code,400)
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='1500',method='bKash').status_code,200)
        order.refresh_from_db()
        self.assertEqual(order.paid_total,2000)
        self.assertEqual(self.client.get('/api/settlements/').json()['receivables'],[])
        self.assertEqual(JournalEntry.objects.get(debit='Bank / mobile payments',credit='Accounts receivable').amount,1500)

    def test_collections_only_apply_to_shipped_or_delivered_online_orders(self):
        pending=create_order([{'product':self.product.pk,'quantity':1}],'Online','COD')
        pos=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        self.assertEqual(self.settle(kind='collection',target=pending.pk,amount='1').status_code,400)
        self.assertEqual(self.settle(kind='collection',target=pos.pk,amount='1').status_code,400)

    def test_supplier_payment_requires_receipt_and_respects_the_payable(self):
        purchase=Purchase.objects.get(pk=self.post('/api/action/',{'action':'purchase','supplier':self.supplier.pk,'lines':[{'product':self.product.pk,'quantity':10,'cost':'80'}]}).json()['id'])
        self.assertEqual(self.settle(kind='supplier_payment',target=purchase.pk,amount='100').status_code,400)
        self.assertEqual(self.post('/api/action/',{'action':'receive','id':purchase.pk}).status_code,200)
        self.assertEqual(self.client.get('/api/settlements/').json()['payables'][0]['due'],'800.00')
        self.assertEqual(self.settle(kind='supplier_payment',target=purchase.pk,amount='900').status_code,400)
        self.assertEqual(self.settle(kind='supplier_payment',target=purchase.pk,amount='800',method='Bank').status_code,200)
        purchase.refresh_from_db()
        self.assertEqual(purchase.paid_total,800)
        self.assertEqual(JournalEntry.objects.get(debit='Accounts payable',credit='Bank / mobile payments').amount,800)
        self.assertEqual(self.client.get('/api/settlements/').json()['payables'],[])

    def test_returns_split_reversal_between_receivable_and_refund_liability(self):
        order=self.shipped_order()
        self.settle(kind='collection',target=order.pk,amount='500')
        transition_order(order.pk,'Delivered',self.owner)
        transition_order(order.pk,'Returned',self.owner)
        self.assertEqual(JournalEntry.objects.get(debit='Sales revenue',credit='Accounts receivable',reference__contains=order.number).amount,1500)
        self.assertEqual(JournalEntry.objects.get(debit='Sales revenue',credit='Customer refunds payable').amount,500)
        refunds=self.client.get('/api/settlements/').json()['refunds']
        self.assertEqual((refunds[0]['number'],refunds[0]['due']),(order.number,'500.00'))
        self.assertEqual(self.settle(kind='refund',target=order.pk,amount='501').status_code,400)
        self.assertEqual(self.settle(kind='refund',target=order.pk,amount='500').status_code,200)
        order.refresh_from_db()
        self.assertEqual(order.refunded_total,500)
        self.assertEqual(JournalEntry.objects.get(debit='Customer refunds payable',credit='Cash').amount,500)

    def test_refund_requires_a_returned_sale(self):
        order=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        self.assertEqual(self.settle(kind='refund',target=order.pk,amount='1').status_code,400)
        transition_order(order.pk,'Returned',self.owner)
        self.assertEqual(JournalEntry.objects.get(debit='Sales revenue',credit='Customer refunds payable').amount,1000)
        self.assertEqual(self.settle(kind='refund',target=order.pk,amount='1000').status_code,200)

    def test_reversal_restores_balance_once_and_never_reverses_a_reversal(self):
        order=self.shipped_order()
        payment=self.settle(kind='collection',target=order.pk,amount='700').json()['id']
        self.assertEqual(self.settle(kind='reversal',reverses=payment,note='').status_code,400)
        response=self.settle(kind='reversal',reverses=payment,note='Entered against the wrong order')
        self.assertEqual(response.status_code,200,response.content)
        order.refresh_from_db()
        self.assertEqual(order.paid_total,0)
        self.assertEqual(JournalEntry.objects.get(debit='Accounts receivable',credit='Cash').amount,700)
        self.assertTrue(self.client.get('/api/settlements/').json()['entries'][1]['reversed'])
        self.assertEqual(self.settle(kind='reversal',reverses=payment,note='Again').status_code,400)
        self.assertEqual(self.settle(kind='reversal',reverses=response.json()['id'],note='Undo the undo').status_code,400)

    def test_collection_cannot_be_reversed_after_the_sale_is_returned(self):
        order=self.shipped_order()
        payment=self.settle(kind='collection',target=order.pk,amount='2000').json()['id']
        transition_order(order.pk,'Delivered',self.owner)
        transition_order(order.pk,'Returned',self.owner)
        self.assertEqual(self.settle(kind='reversal',reverses=payment,note='Too late').status_code,400)

    def test_settlement_validation_and_permissions(self):
        order=self.shipped_order()
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='0').status_code,400)
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='10',method='Crypto').status_code,400)
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='10',reference='').status_code,400)
        self.assertEqual(self.settle(kind='collection',target=order.pk,amount='10',request_id='not-a-uuid').status_code,400)
        self.assertEqual(self.settle(kind='collection',target=999999,amount='10').status_code,400)
        cashier=self.staff('cashier','Cashier')
        self.assertEqual(cashier.get('/api/settlements/').status_code,403)
        self.assertEqual(self.settle(cashier,kind='collection',target=order.pk,amount='10').status_code,403)
        accountant=self.staff('books','Accountant')
        self.assertEqual(accountant.get('/api/settlements/').status_code,200)
        self.assertEqual(self.settle(accountant,kind='collection',target=order.pk,amount='10').status_code,200)
        self.assertEqual(Settlement.objects.get().staff.username,'books')


class ActivityTests(OperationsTestCase):
    def test_activity_requires_finance_access_and_supports_search_and_paging(self):
        self.assertEqual(self.staff('cashier','Cashier').get('/api/activity/').status_code,403)
        for i in range(60): AuditLog.objects.create(staff=self.owner,action=f'Bulk action {i}',detail='')
        AuditLog.objects.create(staff=self.owner,action='Needle',detail='haystack')
        page=self.client.get('/api/activity/').json()
        self.assertEqual(len(page['entries']),50)
        self.assertIsNotNone(page['next'])
        rest=self.client.get('/api/activity/',{'before':page['next']}).json()
        self.assertGreater(len(rest['entries']),0)
        self.assertEqual(len({e['id'] for e in page['entries']} & {e['id'] for e in rest['entries']}),0)
        self.assertEqual([e['action'] for e in self.client.get('/api/activity/',{'q':'haystack'}).json()['entries']],['Needle'])
        self.assertEqual(self.client.get('/api/activity/',{'before':'x'}).status_code,400)

    def test_sign_in_and_sign_out_are_recorded_without_passwords(self):
        client=Client()
        self.assertEqual(client.post('/login/',{'username':'owner','password':'wrong'}).status_code,200)
        client.post('/login/',{'username':'owner','password':'test'})
        client.post('/logout/')
        actions=list(AuditLog.objects.filter(action__in=['Sign-in failed','Signed in','Signed out']).order_by('pk').values_list('action',flat=True))
        self.assertEqual(actions[-3:],['Sign-in failed','Signed in','Signed out'])
        self.assertNotIn('wrong',AuditLog.objects.get(action='Sign-in failed').detail)
        self.assertNotIn('test',AuditLog.objects.filter(action='Signed in').latest('pk').detail)


class ReservationExpiryTests(OperationsTestCase):
    def expired_order(self,quantity=30):
        order=create_order([{'product':self.product.pk,'quantity':quantity}],'Online','COD')
        Order.objects.filter(pk=order.pk).update(reservation_expires_at=timezone.now()-timedelta(minutes=1))
        return order

    def test_online_orders_receive_a_configurable_deadline(self):
        order=create_order([{'product':self.product.pk,'quantity':1}],'Online','COD')
        self.assertAlmostEqual(order.reservation_expires_at,timezone.now()+timedelta(hours=24),delta=timedelta(minutes=1))
        profile=self.client.get('/api/state/').json()['profile']
        self.assertEqual(self.post('/api/content/',{'kind':'profile',**profile,'reservation_hours':200}).status_code,400)
        self.assertEqual(self.post('/api/content/',{'kind':'profile',**profile,'reservation_hours':2}).status_code,200)
        self.assertAlmostEqual(create_order([{'product':self.product.pk,'quantity':1}],'Online','COD').reservation_expires_at,timezone.now()+timedelta(hours=2),delta=timedelta(minutes=1))
        self.assertIsNone(create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash').reservation_expires_at)

    def test_expiry_releases_stock_once_and_leaves_an_audit_trail(self):
        order=self.expired_order()
        self.assertEqual(expire_due_reservations(),1)
        self.assertEqual(expire_due_reservations(),0)
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual((order.status,order.reservation_expires_at,self.product.reserved,self.product.available),('Expired',None,0,30))
        self.assertEqual(StockMovement.objects.filter(kind='Reservation expired',reference=order.number).count(),1)
        self.assertTrue(AuditLog.objects.filter(action=f'{order.number}: Pending → Expired').exists())
        with self.assertRaises(ValueError): transition_order(order.pk,'Confirmed',self.owner)

    def test_confirmed_orders_never_expire_and_stale_pending_orders_cannot_be_confirmed(self):
        order=create_order([{'product':self.product.pk,'quantity':1}],'Online','COD')
        transition_order(order.pk,'Confirmed',self.owner)
        Order.objects.filter(pk=order.pk).update(reservation_expires_at=timezone.now()-timedelta(days=1))
        self.assertEqual(expire_due_reservations(),0)
        stale=self.expired_order(1)
        with self.assertRaises(ValueError): transition_order(stale.pk,'Confirmed',self.owner)
        order.refresh_from_db()
        self.assertEqual(order.status,'Confirmed')

    def test_public_reads_report_expiry_without_writing_and_writes_sweep_first(self):
        order=self.expired_order()
        tracked=Client().get('/api/track/',{'id':str(order.tracking_id)}).json()
        self.assertEqual(tracked['status'],'Expired')
        self.assertEqual(Order.objects.get(pk=order.pk).status,'Pending')
        self.assertEqual(self.client.get('/api/state/').json()['orders'][0]['status'],'Expired')
        response=Client().post('/api/checkout/',json.dumps({'name':'Buyer','phone':'01712345678','address':'Dhaka','items':[{'product':self.product.pk,'quantity':30}]}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(Order.objects.get(pk=order.pk).status,'Expired')

    def test_management_command_releases_overdue_reservations(self):
        self.expired_order(5)
        out=io.StringIO()
        call_command('expire_reservations',stdout=out)
        self.assertIn('Expired 1 reservations.',out.getvalue())
        self.product.refresh_from_db()
        self.assertEqual(self.product.reserved,0)


class ProductionPlumbingTests(OperationsTestCase):
    def test_health_endpoint_is_public_and_reports_database(self):
        response=Client().get('/healthz/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['database'],'ok')
        self.assertEqual(response['Cache-Control'],'max-age=0, no-cache, no-store, must-revalidate, private')

    def test_api_csrf_failures_are_json(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        response=client.post('/api/action/',json.dumps({'action':'sale'}),content_type='application/json')
        self.assertEqual(response.status_code,403)
        self.assertIn('CSRF',response.json()['error'])

    def test_sales_export_includes_payment_columns(self):
        order=self.shipped_order()
        self.settle(kind='collection',target=order.pk,amount='500')
        rows=b''.join(self.client.get('/api/export/').streaming_content).decode().splitlines()
        self.assertEqual(rows[0],'Order,Date,Channel,Status,Payment,Customer,Items,Total BDT,Paid BDT,Refunded BDT,Due BDT')
        self.assertTrue(rows[1].endswith(',2000.00,500.00,0.00,1500.00'))

    def test_checkout_validates_phone_numbers(self):
        body={'name':'Buyer','address':'Dhaka','items':[{'product':self.product.pk,'quantity':1}]}
        self.assertEqual(Client().post('/api/checkout/',json.dumps({**body,'phone':'call me'}),content_type='application/json').status_code,400)
        self.assertEqual(Client().post('/api/checkout/',json.dumps({**body,'phone':'+880 1712-345678'}),content_type='application/json').status_code,200)

    @override_settings(STORE_THROTTLES={'checkout':(2,60),'login':(2,60),'review':(5,3600),'wishlist':(60,60),'track':(60,60),'upload':(30,3600)})
    def test_public_endpoints_and_failed_logins_are_rate_limited(self):
        body=json.dumps({'name':'Buyer','phone':'01712345678','address':'Dhaka','items':[{'product':self.product.pk,'quantity':1}]})
        codes=[Client().post('/api/checkout/',body,content_type='application/json').status_code for _ in range(3)]
        self.assertEqual(codes,[200,200,429])
        for _ in range(2): Client().post('/login/',{'username':'owner','password':'wrong'})
        blocked=Client().post('/login/',{'username':'owner','password':'test'})
        self.assertEqual(blocked.status_code,429)
        self.assertContains(blocked,'Too many sign-in attempts',status_code=429)

    def test_state_payload_no_longer_ships_the_audit_log(self):
        self.assertNotIn('audit',self.client.get('/api/state/').json())

class BackupRestoreTests(TransactionTestCase):
    """The SQLite online-backup API waits for open write transactions, so this runs outside TestCase's wrapping transaction."""
    def setUp(self):
        self.owner=User.objects.create_user('owner',password='test',is_staff=True,is_superuser=True)
        self.product=Product.objects.create(name='Monitor',sku='MON',barcode='MON123',category='Displays',price=1000,cost=500,stock=30)

    def test_backup_and_restore_round_trip(self):
        order=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        with tempfile.TemporaryDirectory() as folder:
            media=Path(folder)/'media'/'uploads'
            media.mkdir(parents=True)
            (media/'logo.png').write_bytes(b'png')
            with override_settings(MEDIA_ROOT=str(Path(folder)/'media')):
                out=io.StringIO()
                call_command('backup_store',destination=folder,stdout=out)
            archive=next(Path(folder).glob('altab-backup-*.zip'))
            with zipfile.ZipFile(archive) as bundle:
                names=set(bundle.namelist())
                manifest=json.loads(bundle.read('manifest.json'))
            self.assertIn('db.sqlite3',names)
            self.assertIn('media/uploads/logo.png',names)
            self.assertIn('0004_backfill_order_snapshots_and_payments',' '.join(manifest['migrations']))
            target=Path(folder)/'restored'/'db.sqlite3'
            restored_media=Path(folder)/'restored-media'
            self.assertRaises(SystemExit,call_command,'restore_store',str(archive),target=str(target),media_root=str(restored_media),stdout=io.StringIO(),stderr=io.StringIO())
            call_command('restore_store',str(archive),target=str(target),media_root=str(restored_media),yes=True,stdout=io.StringIO())
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('select count(*) from store_order where id=?',[order.pk]).fetchone()[0],1)
            self.assertEqual((restored_media/'uploads'/'logo.png').read_bytes(),b'png')
