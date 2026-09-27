import json
from decimal import Decimal
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.test import TestCase, Client
from .models import Product, Order, Customer, Supplier, Purchase, JournalEntry, StockMovement
from .services import create_order, transition_order


class StoreFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner=User.objects.create_user('owner',password='test-password',is_staff=True,is_superuser=True)
        self.product=Product.objects.create(name='Test keyboard',sku='KEY-01',barcode='123456789012',category='Peripherals',price=100,cost=60,stock=10)
        self.client.force_login(self.owner)

    def action(self, data):
        return self.client.post('/api/action/',json.dumps(data),content_type='application/json')

    def test_pos_sale_decrements_stock_and_posts_revenue_and_cogs(self):
        response=self.action({'action':'sale','items':[{'product':self.product.pk,'quantity':2}],'payment':'Cash','discount':10})
        self.assertEqual(response.status_code,200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock,8)
        self.assertEqual(Order.objects.get().total,190)
        self.assertEqual(JournalEntry.objects.get(credit='Sales revenue').amount,190)
        self.assertEqual(JournalEntry.objects.get(debit='Cost of goods sold').amount,120)

    def test_online_reservation_blocks_overselling_then_cancellation_releases(self):
        order=create_order([{'product':self.product.pk,'quantity':8}],'Online','COD')
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock,self.product.reserved,self.product.available),(10,8,2))
        with self.assertRaises(ValueError):create_order([{'product':self.product.pk,'quantity':3}],'POS','Cash')
        self.assertEqual(Order.objects.count(),1)
        transition_order(order.pk,'Cancelled',self.owner)
        self.product.refresh_from_db()
        self.assertEqual(self.product.available,10)

    def test_order_lifecycle_and_return(self):
        order=create_order([{'product':self.product.pk,'quantity':3}],'Online','COD')
        for status in ['Confirmed','Packed','Shipped','Delivered']:transition_order(order.pk,status,self.owner)
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock,self.product.reserved),(7,0))
        self.assertEqual(JournalEntry.objects.get(debit='Accounts receivable').amount,300)
        transition_order(order.pk,'Returned',self.owner)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock,10)
        with self.assertRaises(ValueError):transition_order(order.pk,'Returned',self.owner)

    def test_failed_multiline_sale_rolls_back_all_changes(self):
        second=Product.objects.create(name='Mouse',sku='M1',barcode='M1',category='Mouse',price=50,cost=20,stock=0)
        with self.assertRaises(ValueError):create_order([{'product':self.product.pk,'quantity':1},{'product':second.pk,'quantity':1}],'POS','Cash')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock,10)
        self.assertEqual(Order.objects.count(),0)
        self.assertEqual(StockMovement.objects.count(),0)

    def test_purchase_receipt_is_once_only_with_weighted_average(self):
        supplier=Supplier.objects.create(name='Supplier')
        purchase=Purchase.objects.get(pk=self.action({'action':'purchase','supplier':supplier.pk,'product':self.product.pk,'quantity':10,'cost':80}).json()['id'])
        self.assertEqual(self.action({'action':'receive','id':purchase.pk}).status_code,200)
        self.assertEqual(self.action({'action':'receive','id':purchase.pk}).status_code,400)
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock,self.product.cost),(20,Decimal('70')))
        self.assertEqual(JournalEntry.objects.get(credit='Accounts payable').amount,800)

    def test_stock_adjustment_preserves_reservations(self):
        create_order([{'product':self.product.pk,'quantity':8}],'Online','COD')
        self.assertEqual(self.action({'action':'adjust','product':self.product.pk,'quantity':-3,'reason':'Damaged'}).status_code,400)
        self.assertEqual(self.action({'action':'adjust','product':self.product.pk,'quantity':-2,'reason':'Damaged'}).status_code,200)

    def test_cashier_cannot_adjust_stock_or_read_costs(self):
        cashier=User.objects.create_user('cashier',password='test-password',is_staff=True)
        cashier.groups.add(Group.objects.create(name='Cashier'))
        self.client.force_login(cashier)
        self.assertEqual(self.action({'action':'adjust','product':self.product.pk,'quantity':1,'reason':'Count'}).status_code,403)
        response=self.client.get('/api/state/').json()
        self.assertNotIn('cost',response['products'][0])
        self.assertEqual(response['journal'],[])

    def test_public_catalog_hides_costs_and_checkout_uses_server_prices(self):
        self.client.logout()
        self.assertEqual(self.client.get('/api/state/').status_code,403)
        self.assertNotIn('cost',self.client.get('/api/catalog/').json()['products'][0])
        response=self.client.post('/api/checkout/',json.dumps({'name':'Buyer','phone':'01712345678','address':'Dhaka','items':[{'product':self.product.pk,'quantity':2,'price':1}],'total':1}),content_type='application/json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['total'],'200.00')
        tracking=self.client.get('/api/track/',{'id':response.json()['tracking_id']}).json()
        self.assertEqual(tracking['status'],'Pending')
        self.assertNotIn('customer',tracking)

    def test_invalid_discount_and_fractional_quantity_rejected(self):
        for body in [{'discount':201,'items':[{'product':self.product.pk,'quantity':2}]},{'discount':0,'items':[{'product':self.product.pk,'quantity':1.5}]},{'discount':'NaN','items':[{'product':self.product.pk,'quantity':1}]}]:
            self.assertEqual(self.action({'action':'sale','payment':'Cash',**body}).status_code,400)
        self.assertEqual(Order.objects.count(),0)

    def test_duplicate_lines_combined_before_stock_check(self):
        with self.assertRaises(ValueError):create_order([{'product':self.product.pk,'quantity':6},{'product':self.product.pk,'quantity':6}],'POS','Cash')
        self.assertEqual(Order.objects.count(),0)

    def test_csrf_enforced_and_staff_required_for_receipts(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post('/api/action/',json.dumps({'action':'sale'}),content_type='application/json').status_code,403)
        shopper=User.objects.create_user('shopper',password='test-password')
        self.client.force_login(shopper)
        self.assertEqual(self.client.get('/receipt/1/').status_code,403)

    def test_pages_render(self):
        for path in ['/','/shop/','/login/']:
            self.assertEqual(self.client.get(path).status_code,200)
