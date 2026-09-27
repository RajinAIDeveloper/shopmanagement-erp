"""VAT, split tenders, dues and store credit, partial returns and exchanges, and cash-drawer shifts."""
import json
import uuid
from decimal import Decimal
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from .models import Product, Customer, Order, OrderPayment, SaleReturn, CreditTransaction, Shift, Settlement, JournalEntry, StockMovement, StoreProfile, Expense
from .services import create_order, transition_order
from .selling import process_return, open_shift, close_shift, shift_movement, shift_summary


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SellingTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner=User.objects.create_user('owner',password='test',is_staff=True,is_superuser=True)
        self.client.force_login(self.owner)
        self.monitor=Product.objects.create(name='Monitor',sku='MON',barcode='MON1',category='Displays',price=1150,cost=600,stock=20,wholesale_price=1000,min_price=900)
        self.cable=Product.objects.create(name='Cable',sku='CAB',barcode='CAB1',category='Accessories',price=230,cost=100,stock=50,vat_exempt=True)
        self.customer=Customer.objects.create(name='Arif',phone='01712345678',credit_limit=5000)

    def post(self,path,data,client=None):
        return (client or self.client).post(path,json.dumps(data),content_type='application/json')

    def sale(self,items,client=None,**extra):
        return self.post('/api/action/',{'action':'sale','items':items,'payment':'Cash',**extra},client)

    def staff(self,username,group):
        user=User.objects.create_user(username,password='test',is_staff=True)
        user.groups.add(Group.objects.get_or_create(name=group)[0])
        client=Client()
        client.force_login(user)
        return client

    def balance(self,account):
        debit=sum(j.amount for j in JournalEntry.objects.filter(debit=account))
        credit=sum(j.amount for j in JournalEntry.objects.filter(credit=account))
        return debit-credit


class VatTests(SellingTestCase):
    def test_inclusive_pricing_keeps_totals_and_splits_vat_out_of_revenue(self):
        order=create_order([{'product':self.monitor.pk,'quantity':2},{'product':self.cable.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        self.assertEqual(order.total,Decimal('2530.00'))
        self.assertEqual(order.tax_total,Decimal('300.00'))
        self.assertEqual({l.product_id:l.tax_rate for l in order.lines.all()},{self.monitor.pk:Decimal('15.00'),self.cable.pk:Decimal('0.00')})
        self.assertEqual(self.balance('Sales revenue'),Decimal('-2230.00'))
        self.assertEqual(self.balance('VAT payable'),Decimal('-300.00'))
        self.assertEqual(self.balance('Cash'),Decimal('2530.00'))

    def test_exclusive_pricing_adds_vat_and_discount_reduces_it_proportionally(self):
        StoreProfile.objects.filter(pk=StoreProfile.current().pk).update(prices_include_vat=False)
        order=create_order([{'product':self.monitor.pk,'quantity':1}],'POS','Cash',discount=150,staff=self.owner)
        self.assertEqual(order.total,Decimal('1150.00'))
        self.assertEqual(order.tax_total,Decimal('150.00'))
        self.assertEqual(self.balance('Sales revenue'),Decimal('-1000.00'))

    def test_online_orders_recognise_vat_at_shipping(self):
        order=create_order([{'product':self.monitor.pk,'quantity':1}],'Online','COD')
        self.assertEqual(order.tax_total,Decimal('150.00'))
        self.assertFalse(JournalEntry.objects.filter(credit='VAT payable').exists())
        for status in ['Confirmed','Packed','Shipped']: transition_order(order.pk,status,self.owner)
        self.assertEqual(self.balance('VAT payable'),Decimal('-150.00'))
        self.assertEqual(self.balance('Accounts receivable'),Decimal('1150.00'))

    def test_settings_control_vat_and_receipt_shows_it(self):
        self.assertEqual(self.post('/api/content/',{'kind':'profile','vat_rate':'101'}).status_code,400)
        self.assertEqual(self.post('/api/content/',{'kind':'profile','vat_rate':'7.5','vat_registration':'BIN-123','receipt_footer':'Come again!'}).status_code,200)
        profile=StoreProfile.current()
        self.assertEqual((profile.vat_rate,profile.vat_registration,profile.name),(Decimal('7.50'),'BIN-123','Altab Tech'))
        order=create_order([{'product':self.monitor.pk,'quantity':1}],'POS','Cash',staff=self.owner)
        receipt=self.client.get(f'/receipt/{order.pk}/').content.decode()
        for expected in ['Includes VAT 7.50%','BIN-123','Come again!','Paid by Cash']: self.assertIn(expected,receipt)


class TenderTests(SellingTestCase):
    def test_split_tender_with_cash_change(self):
        response=self.sale([{'product':self.monitor.pk,'quantity':1}],tenders=[{'method':'bKash','amount':'500','reference':'TX1'},{'method':'Cash','amount':'1000'}])
        self.assertEqual(response.status_code,200,response.content)
        order=response.json()['order']
        self.assertEqual((order['paid_total'],order['change'],order['payment']),(1150.0,350.0,'Split'))
        payments={p.method:p.amount for p in OrderPayment.objects.filter(order_id=order['id'])}
        self.assertEqual(payments,{'bKash':Decimal('500.00'),'Cash':Decimal('650.00')})
        self.assertEqual(self.balance('Bank / mobile payments'),Decimal('500.00'))
        self.assertEqual(self.balance('Cash'),Decimal('650.00'))

    def test_overpayment_must_be_cash(self):
        self.assertEqual(self.sale([{'product':self.monitor.pk,'quantity':1}],tenders=[{'method':'Card','amount':'2000'}]).status_code,400)
        self.assertEqual(Order.objects.count(),0)

    def test_walk_in_customers_must_pay_in_full(self):
        response=self.sale([{'product':self.monitor.pk,'quantity':1}],tenders=[{'method':'Cash','amount':'100'}])
        self.assertEqual(response.status_code,400)
        self.assertIn('Walk-in',response.json()['error'])

    def test_account_customer_can_owe_up_to_the_credit_limit(self):
        response=self.sale([{'product':self.monitor.pk,'quantity':4}],customer=self.customer.pk,tenders=[{'method':'Cash','amount':'1000'}])
        self.assertEqual(response.status_code,200,response.content)
        order=Order.objects.get()
        self.assertEqual((order.paid_total,order.due,order.payment),(Decimal('1000.00'),Decimal('3600.00'),'Cash'))
        self.assertEqual(self.balance('Accounts receivable'),Decimal('3600.00'))
        self.assertEqual(self.sale([{'product':self.monitor.pk,'quantity':2}],customer=self.customer.pk,tenders=[]).status_code,400)
        settlements=self.client.get('/api/settlements/').json()
        self.assertEqual(settlements['receivables'][0]['due'],'3600.00')
        collect=self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'kind':'collection','target':order.pk,'amount':'3600','method':'Cash','reference':'Paid at counter'})
        self.assertEqual(collect.status_code,200,collect.content)
        self.assertEqual(self.client.get('/api/state/').json()['customers'][0]['outstanding'],0)

    def test_store_credit_can_be_tendered_and_never_overdrawn(self):
        self.assertEqual(self.post('/api/action/',{'action':'credit','customer':self.customer.pk,'amount':'300','reason':'Goodwill'}).status_code,200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.credit_balance,Decimal('300.00'))
        self.assertEqual(self.sale([{'product':self.cable.pk,'quantity':1}],customer=self.customer.pk,tenders=[{'method':'Store credit','amount':'230'}]).status_code,200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.credit_balance,Decimal('70.00'))
        self.assertEqual(self.balance('Store credit liability'),Decimal('-70.00'))
        self.assertEqual(self.sale([{'product':self.cable.pk,'quantity':1}],customer=self.customer.pk,tenders=[{'method':'Store credit','amount':'230'}]).status_code,400)
        history=self.client.get(f'/api/customers/{self.customer.pk}/').json()
        self.assertEqual([h['kind'] for h in history['credit_history']],['redeemed','adjustment'])

    def test_wholesale_tier_and_price_floors(self):
        Customer.objects.filter(pk=self.customer.pk).update(pricing_tier='wholesale')
        response=self.sale([{'product':self.monitor.pk,'quantity':1}],customer=self.customer.pk)
        self.assertEqual(response.json()['order']['total'],1000.0)
        cashier=self.staff('cashier','Cashier')
        self.assertEqual(self.sale([{'product':self.monitor.pk,'quantity':1,'price':'850'}],cashier).status_code,400)
        self.assertEqual(self.sale([{'product':self.monitor.pk,'quantity':1,'price':'950'}],cashier).status_code,200)
        self.assertEqual(self.sale([{'product':self.monitor.pk,'quantity':1,'price':'850'}]).status_code,200)
        self.assertEqual(self.sale([{'product':self.monitor.pk,'quantity':1,'price':'1'}],customer=self.customer.pk).status_code,200)

    def test_sale_request_id_makes_offline_replay_idempotent(self):
        rid=str(uuid.uuid4())
        first=self.sale([{'product':self.monitor.pk,'quantity':1}],request_id=rid).json()['order']
        second=self.sale([{'product':self.monitor.pk,'quantity':1}],request_id=rid).json()['order']
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(Order.objects.count(),1)
        self.monitor.refresh_from_db()
        self.assertEqual(self.monitor.stock,19)

    def test_customer_editing_and_credit_are_role_gated(self):
        cashier=self.staff('cashier','Cashier')
        self.assertEqual(self.post('/api/action/',{'action':'customer','id':self.customer.pk,'name':'Arif Rahman','phone':'01712345678','email':'','address':'Dhaka'},cashier).status_code,200)
        self.assertEqual(self.post('/api/action/',{'action':'customer','id':self.customer.pk,'name':'Arif Rahman','pricing_tier':'wholesale'},cashier).status_code,400)
        self.assertEqual(self.post('/api/action/',{'action':'credit','customer':self.customer.pk,'amount':'50','reason':'x'},cashier).status_code,403)
        self.assertEqual(self.post('/api/action/',{'action':'customer','id':self.customer.pk,'name':'Arif Rahman','pricing_tier':'wholesale','credit_limit':'9000'}).status_code,200)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.name,self.customer.pricing_tier,self.customer.credit_limit),('Arif Rahman','wholesale',Decimal('9000.00')))


class ReturnTests(SellingTestCase):
    def paid_sale(self,quantity=3,discount=0):
        return Order.objects.get(pk=self.sale([{'product':self.monitor.pk,'quantity':quantity}],discount=discount).json()['order']['id'])

    def test_partial_return_with_cash_refund_restocks_and_reverses_vat(self):
        order=self.paid_sale()
        response=self.post('/api/returns/',{'order':order.pk,'lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'Dead pixels','restock':True,'refund_method':'Cash'})
        self.assertEqual(response.status_code,200,response.content)
        order.refresh_from_db()
        self.monitor.refresh_from_db()
        self.assertEqual((order.status,order.returned_total,order.refunded_total,order.refund_due,self.monitor.stock),('Partially returned',Decimal('1150.00'),Decimal('1150.00'),Decimal('0.00'),18))
        self.assertEqual(order.lines.get().returned_quantity,1)
        self.assertEqual(self.balance('Sales revenue'),Decimal('-2000.00'))
        self.assertEqual(self.balance('VAT payable'),Decimal('-300.00'))
        self.assertEqual(self.balance('Cash'),Decimal('2300.00'))
        self.assertEqual(self.balance('Customer refunds payable'),Decimal('0.00'))
        self.assertEqual(self.balance('Inventory'),Decimal('-1200.00'))
        self.assertTrue(Settlement.objects.filter(kind='refund',method='Cash',order=order).exists())
        self.assertEqual(response.json()['refunded_now'],'1150.00')

    def test_returns_respect_remaining_quantities_and_discounts(self):
        order=self.paid_sale(quantity=2,discount=300)
        line=order.lines.get().pk
        self.assertEqual(self.post('/api/returns/',{'order':order.pk,'lines':[{'line':line,'quantity':3}],'reason':'x'}).status_code,400)
        self.assertEqual(self.post('/api/returns/',{'order':order.pk,'lines':[{'line':line,'quantity':0}],'reason':'x'}).status_code,400)
        self.assertEqual(self.post('/api/returns/',{'order':order.pk,'lines':[{'line':line,'quantity':1}],'reason':'x'}).status_code,200)
        self.assertEqual(self.post('/api/returns/',{'order':order.pk,'lines':[{'line':line,'quantity':1}],'reason':'x'}).status_code,200)
        order.refresh_from_db()
        self.assertEqual((order.status,order.returned_total,order.refund_due),('Returned',Decimal('2000.00'),Decimal('2000.00')))
        self.assertEqual(self.post('/api/returns/',{'order':order.pk,'lines':[{'line':line,'quantity':1}],'reason':'x'}).status_code,400)

    def test_damaged_goods_are_not_restocked(self):
        order=self.paid_sale(quantity=1)
        self.post('/api/returns/',{'order':order.pk,'lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'Broken','restock':False,'refund_method':'later'})
        self.monitor.refresh_from_db()
        self.assertEqual(self.monitor.stock,19)
        self.assertTrue(StockMovement.objects.filter(kind='Return (not restocked)').exists())
        self.assertFalse(JournalEntry.objects.filter(debit='Inventory',credit='Cost of goods sold').exists())

    def test_refund_to_store_credit_requires_a_customer(self):
        walk_in=self.paid_sale(quantity=1)
        self.assertEqual(self.post('/api/returns/',{'order':walk_in.pk,'lines':[{'line':walk_in.lines.get().pk,'quantity':1}],'reason':'x','refund_method':'Store credit'}).status_code,400)
        on_account=Order.objects.get(pk=self.sale([{'product':self.monitor.pk,'quantity':1}],customer=self.customer.pk).json()['order']['id'])
        self.assertEqual(self.post('/api/returns/',{'order':on_account.pk,'lines':[{'line':on_account.lines.get().pk,'quantity':1}],'reason':'x','refund_method':'Store credit'}).status_code,200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.credit_balance,Decimal('1150.00'))
        self.assertEqual(self.balance('Store credit liability'),Decimal('-1150.00'))

    def test_return_of_unpaid_goods_cancels_the_receivable_first(self):
        order=Order.objects.get(pk=self.sale([{'product':self.monitor.pk,'quantity':2}],customer=self.customer.pk,tenders=[{'method':'Cash','amount':'1000'}]).json()['order']['id'])
        self.post('/api/returns/',{'order':order.pk,'lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'x'})
        order.refresh_from_db()
        self.assertEqual((order.due,order.refund_due),(Decimal('150.00'),Decimal('0.00')))
        self.assertEqual(self.balance('Accounts receivable'),Decimal('150.00'))
        self.post('/api/returns/',{'order':order.pk,'lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'x'})
        order.refresh_from_db()
        self.assertEqual((order.due,order.refund_due,order.status),(Decimal('0.00'),Decimal('1000.00'),'Returned'))
        self.assertEqual(self.balance('Accounts receivable'),Decimal('0.00'))
        self.assertEqual(self.balance('Customer refunds payable'),Decimal('-1000.00'))

    def test_full_return_transition_still_works_and_only_managers_return(self):
        order=self.paid_sale(quantity=2)
        cashier=self.staff('cashier','Cashier')
        self.assertEqual(self.post('/api/returns/',{'order':order.pk,'lines':[],'reason':'x'},cashier).status_code,403)
        transition_order(order.pk,'Returned',self.owner)
        order.refresh_from_db()
        self.assertEqual((order.status,order.returned_total,order.refund_due),('Returned',Decimal('2300.00'),Decimal('2300.00')))
        self.assertEqual(SaleReturn.objects.get().kind,'return')

    def test_exchange_applies_credit_to_the_replacement_sale(self):
        order=self.paid_sale(quantity=1)
        response=self.post('/api/returns/',{'order':order.pk,'kind':'exchange','lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'Wrong size'})
        self.assertEqual(response.status_code,200,response.content)
        exchange=response.json()['return']
        self.assertEqual((exchange['kind'],response.json()['refund_due']),('exchange','1150.00'))
        replacement=self.sale([{'product':self.monitor.pk,'quantity':1},{'product':self.cable.pk,'quantity':1}],exchange_return=exchange['id'],tenders=[{'method':'Exchange credit','amount':'1150'},{'method':'Cash','amount':'230'}])
        self.assertEqual(replacement.status_code,200,replacement.content)
        order.refresh_from_db()
        sale_return=SaleReturn.objects.get()
        self.assertEqual((order.refunded_total,order.refund_due,sale_return.exchange_used,sale_return.exchange_order_id),(Decimal('1150.00'),Decimal('0.00'),Decimal('1150.00'),replacement.json()['order']['id']))
        self.assertEqual(self.balance('Customer refunds payable'),Decimal('0.00'))
        self.assertEqual(self.balance('Cash'),Decimal('1380.00'))
        self.assertEqual(self.sale([{'product':self.cable.pk,'quantity':1}],exchange_return=exchange['id'],tenders=[{'method':'Exchange credit','amount':'230'}]).status_code,400)

    def test_exchange_for_a_cheaper_item_leaves_a_refund_balance(self):
        order=self.paid_sale(quantity=1)
        exchange=self.post('/api/returns/',{'order':order.pk,'kind':'exchange','lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'Downgrade'}).json()['return']
        self.assertEqual(self.sale([{'product':self.cable.pk,'quantity':1}],exchange_return=exchange['id'],tenders=[{'method':'Exchange credit','amount':'230'}]).status_code,200)
        order.refresh_from_db()
        self.assertEqual(order.refund_due,Decimal('920.00'))
        self.assertEqual(self.client.get('/api/settlements/').json()['refunds'][0]['due'],'920.00')
        self.assertEqual(self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'kind':'refund','target':order.pk,'amount':'920','method':'Cash','reference':'Cash back'}).status_code,200)

    def test_collection_reversal_is_blocked_once_goods_came_back(self):
        order=Order.objects.get(pk=self.sale([{'product':self.monitor.pk,'quantity':1}],customer=self.customer.pk,tenders=[]).json()['order']['id'])
        payment=self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'kind':'collection','target':order.pk,'amount':'1150','method':'Cash','reference':'Paid'}).json()['id']
        self.post('/api/returns/',{'order':order.pk,'lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'x'})
        self.assertEqual(self.post('/api/settlements/save/',{'request_id':str(uuid.uuid4()),'kind':'reversal','reverses':payment,'note':'oops'}).status_code,400)


class ShiftTests(SellingTestCase):
    def test_shift_tracks_cash_and_posts_variance(self):
        self.assertEqual(self.post('/api/shifts/action/',{'action':'open','opening_float':'500'}).status_code,200)
        self.assertEqual(self.post('/api/shifts/action/',{'action':'open','opening_float':'1'}).status_code,400)
        self.sale([{'product':self.monitor.pk,'quantity':1}],tenders=[{'method':'Cash','amount':'1200'}])
        self.sale([{'product':self.cable.pk,'quantity':1}],tenders=[{'method':'Card','amount':'230'}])
        order=Order.objects.first()
        self.post('/api/returns/',{'order':order.pk,'lines':[{'line':order.lines.get().pk,'quantity':1}],'reason':'x','refund_method':'Cash'})
        self.assertEqual(self.post('/api/shifts/action/',{'action':'movement','kind':'payout','amount':'100','note':'Tea and snacks'}).status_code,200)
        self.assertEqual(self.post('/api/shifts/action/',{'action':'movement','kind':'drop','amount':'99999','note':'x'}).status_code,400)
        summary=self.client.get('/api/shifts/').json()['current']
        self.assertEqual(summary['expected_cash'],'400.00')
        self.assertEqual(summary['tenders'],{'Cash':'1150.00','Card':'230.00'})
        self.assertEqual(summary['refunds'],{'Cash':'1150.00'})
        self.assertTrue(Expense.objects.filter(category='Petty cash',amount=100).exists())
        self.assertEqual(self.post('/api/shifts/action/',{'action':'close','counted_cash':'380'}).status_code,200)
        shift=Shift.objects.get()
        self.assertEqual((shift.expected_cash,shift.counted_cash,shift.variance),(Decimal('400.00'),Decimal('380.00'),Decimal('-20.00')))
        self.assertEqual(self.balance('Cash over / short'),Decimal('20.00'))
        self.assertEqual(Order.objects.filter(shift=shift).count(),2)
        self.assertEqual(self.client.get('/api/shifts/').json()['history'][0]['number'],shift.number)
        self.assertIsNone(self.client.get('/api/state/').json()['shift'])

    def test_shift_can_be_required_before_selling(self):
        self.assertEqual(self.post('/api/content/',{'kind':'profile','require_shift':True}).status_code,200)
        response=self.sale([{'product':self.cable.pk,'quantity':1}])
        self.assertEqual(response.status_code,400)
        self.assertIn('drawer shift',response.json()['error'])
        open_shift(self.owner,0)
        self.assertEqual(self.sale([{'product':self.cable.pk,'quantity':1}]).status_code,200)

    def test_only_cashiers_operate_the_drawer(self):
        accountant=self.staff('books','Accountant')
        self.assertEqual(self.post('/api/shifts/action/',{'action':'open','opening_float':'0'},accountant).status_code,403)
        self.assertEqual(accountant.get('/api/shifts/').status_code,200)
        cashier=self.staff('cashier','Cashier')
        self.assertEqual(self.post('/api/shifts/action/',{'action':'open','opening_float':'0'},cashier).status_code,200)
        self.assertEqual(cashier.get('/api/shifts/').json()['history'],[])
        self.assertEqual(self.post('/api/shifts/action/',{'action':'close','counted_cash':'0'},cashier).status_code,200)
