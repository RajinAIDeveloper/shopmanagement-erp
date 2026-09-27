import json
import uuid
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.models import User, Group
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import Account, JournalEntry, JournalVoucher, FiscalPeriod, Product, Order
from .accounting import financial_report, post_pair
from .services import create_order


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AccountingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('ledger-owner', password='test', is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)
        self.day = timezone.localdate()
        self.cash = Account.objects.get(name='Cash')
        self.equity = Account.objects.get(name='Opening equity')
        self.expense = Account.objects.get(name='Operating expenses')

    def post(self, **d): return self.client.post('/api/accounting/action/', json.dumps(d), content_type='application/json')

    def voucher(self, **extra):
        return {'action':'voucher','request_id':str(uuid.uuid4()),'reference':'opening-test','memo':'Opening float','date':str(self.day), 'lines':[{'account':self.cash.pk,'debit':'1000'},{'account':self.equity.pk,'credit':'1000'}], **extra}

    def test_balanced_voucher_replay_reversal_and_unbalanced_rollback(self):
        d = self.voucher()
        r = self.post(**d); self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.post(**d).json()['id'], r.json()['id'])
        self.assertEqual(JournalEntry.objects.count(), 1)
        bad = self.voucher(lines=[{'account':self.cash.pk,'debit':100},{'account':self.equity.pk,'credit':99}])
        self.assertEqual(self.post(**bad).status_code, 400)
        self.assertEqual(JournalVoucher.objects.count(), 1)
        reverse = self.voucher(reverses=r.json()['id'], reference='reverse', memo='Correct mistaken amount')
        self.assertEqual(self.post(**reverse).status_code, 200)
        self.assertEqual(financial_report(self.day,self.day)['assets'], 0)
        self.assertEqual(self.post(**self.voucher(reverses=r.json()['id'])).status_code, 400)

    def test_closed_period_blocks_operational_sale_and_rolls_back_stock(self):
        p = Product.objects.create(name='Test',sku='TEST',barcode='TEST',category='Test',price=115,cost=50,stock=10)
        FiscalPeriod.objects.create(name='Closed',start=self.day,end=self.day,closed=True)
        with self.assertRaisesMessage(ValueError, 'closed'): create_order([{'product':p.pk,'quantity':1}], 'POS','Cash',staff=self.owner)
        p.refresh_from_db(); self.assertEqual(p.stock,10); self.assertFalse(Order.objects.exists())
        self.assertEqual(self.post(**self.voucher()).status_code,400)

    def test_period_overlap_reopen_and_permissions(self):
        r = self.post(action='period',name='Period',start=str(self.day),end=str(self.day))
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.post(action='period',name='Overlap',start=str(self.day),end=str(self.day)).status_code,400)
        self.assertEqual(self.post(action='period',id=r.json()['id'],closed=True).status_code,200)
        self.assertEqual(self.post(action='period',id=r.json()['id'],closed=False).status_code,200)
        cashier=User.objects.create_user('cashier',password='test',is_staff=True)
        cashier.groups.add(Group.objects.get_or_create(name='Cashier')[0]); self.client.force_login(cashier)
        self.assertEqual(self.client.get('/api/accounting/').status_code,403)
        self.assertEqual(self.post(**self.voucher()).status_code,403)

    def test_statements_balance_and_flow_classification(self):
        yesterday=self.day-timedelta(days=1)
        post_pair('capital','Cash','Opening equity',Decimal(1000),yesterday)
        post_pair('income','Cash','Sales revenue',Decimal(230),self.day)
        post_pair('tax','Sales revenue','VAT payable',Decimal(30),self.day)
        post_pair('cost','Operating expenses','Cash',Decimal(20),self.day)
        report=financial_report(self.day,self.day)
        self.assertEqual(report['income'],200); self.assertEqual(report['profit'],180)
        self.assertEqual(report['balance_difference'],0)
        self.assertEqual(report['cash_opening'],1000); self.assertEqual(report['cash_closing'],1210)
        self.assertEqual(report['cash_flow']['operating'],210)
        self.assertEqual(report['retained_earnings'],180)
        self.assertEqual(self.client.get('/api/accounting/?format=csv').status_code,200)

    def test_reconciliation_requires_exact_match_and_all_lines(self):
        entry=post_pair('deposit','Cash','Opening equity',Decimal(100),self.day)
        r=self.post(action='reconciliation',account=self.cash.pk,start=str(self.day),end=str(self.day),opening=0,closing=100,reference='statement',lines=[{'date':str(self.day),'description':'deposit','amount':100}])
        self.assertEqual(r.status_code,200,r.content); pk=r.json()['id']
        self.assertEqual(self.post(action='reconcile',id=pk).status_code,400)
        data=self.client.get(f'/api/accounting/?reconciliation={pk}').json()['reconciliation']; line=data['lines'][0]['id']
        wrong=post_pair('other','Cash','Opening equity',Decimal(50),self.day)
        self.assertEqual(self.post(action='match',id=pk,line=line,entry=wrong.pk).status_code,400)
        self.assertEqual(self.post(action='match',id=pk,line=line,entry=entry.pk).status_code,200)
        self.assertEqual(self.post(action='reconcile',id=pk).status_code,200)
        self.assertEqual(self.post(action='match',id=pk,line=line).status_code,400)

    def test_control_accounts_and_future_dates_rejected(self):
        inventory=Account.objects.get(name='Inventory')
        self.assertEqual(self.post(**self.voucher(lines=[{'account':inventory.pk,'debit':100},{'account':self.equity.pk,'credit':100}])).status_code,400)
        self.assertEqual(self.post(**self.voucher(date=str(self.day+timedelta(days=1)))).status_code,400)

    def test_custom_cash_account_and_used_account_immutable(self):
        r=self.post(action='account',code='1020',name='Business bank',kind='asset',cash=True,cash_flow='operating')
        self.assertEqual(r.status_code,200)
        post_pair('fund','Business bank','Opening equity',Decimal(50))
        self.assertEqual(self.post(action='account',id=r.json()['id'],code='1020',name='Renamed bank',kind='asset').status_code,400)
