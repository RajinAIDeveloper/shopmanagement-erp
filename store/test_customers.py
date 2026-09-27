import json
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .models import Customer, Product, Order, Coupon, CouponUse, ShippingZone, StoreProfile, WishlistItem, Review
from .services import transition_order, expire_due_reservations
from .selling import process_return
from .customers import loyalty_post


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CustomerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner=User.objects.create_user('owner',password='test',is_staff=True,is_superuser=True)
        self.product=Product.objects.create(name='Camera',sku='CAM',barcode='CAM',category='Camera',price=1150,cost=500,stock=30)
        self.user=User.objects.create_user('buyer@example.com',email='buyer@example.com',password='Strong-demo-2914')
        self.customer=Customer.objects.create(name='Buyer',user=self.user,email=self.user.email,phone='01712345678')
        self.client.force_login(self.user)

    def post(self,path,d,client=None): return (client or self.client).post(path,json.dumps(d),content_type='application/json')
    def checkout(self,**extra): return self.post('/api/checkout/',{'name':'Buyer','phone':'01712345678','address':'Dhaka','items':[{'product':self.product.pk,'quantity':1}],**extra})

    def test_register_does_not_claim_guest_orders_and_validates_password(self):
        guest=Customer.objects.create(name='Guest',email='new@example.com')
        web=Client()
        self.assertEqual(self.post('/api/account/action/',{'action':'register','name':'New','email':'new@example.com','password':'123'},web).status_code,400)
        r=self.post('/api/account/action/',{'action':'register','name':'New','email':'new@example.com','password':'Strong-demo-8275'},web)
        self.assertEqual(r.status_code,200,r.content)
        self.assertNotEqual(web.get('/api/account/').json()['account']['id'],guest.pk)
        self.assertEqual(web.get('/api/state/').status_code,403)

    def test_wishlist_follows_account_between_sessions(self):
        r=self.post('/api/wishlist/',{'product':self.product.pk,'saved':True})
        self.assertEqual(r.status_code,200)
        second=Client();second.force_login(self.user)
        self.assertEqual(second.get('/api/catalog/').json()['wishlist'],[self.product.pk])
        self.assertEqual(Client().get('/api/catalog/').json()['wishlist'],[])

    def test_coupon_quote_checkout_ship_and_returns_keep_delivery_revenue(self):
        zone=ShippingZone.objects.create(name='Dhaka',charge=115)
        Coupon.objects.create(code='TEN',kind='percent',value=10,max_uses=1,per_customer=1)
        r=self.checkout(zone=zone.pk,coupon='ten');self.assertEqual(r.status_code,200,r.content)
        o=Order.objects.get(tracking_id=r.json()['tracking_id'])
        self.assertEqual(o.total,Decimal(1150)); self.assertEqual(o.shipping_total,115)
        self.assertEqual(o.tax_total,Decimal(150));self.assertEqual(o.customer_id,self.customer.pk)
        self.assertEqual(self.checkout(zone=zone.pk,coupon='TEN').status_code,400)
        for status in ['Confirmed','Packed','Shipped','Delivered']: transition_order(o.pk,status,self.owner)
        process_return(o.pk,None,'Return',True,'later','return',self.owner)
        o.refresh_from_db();self.assertEqual(o.net_total,Decimal(115))

    def test_cancel_restores_points_and_releases_coupon_once(self):
        p=StoreProfile.current();p.loyalty_enabled=True;p.save()
        loyalty_post(self.customer,None,50,'Opening rewards','initial')
        Coupon.objects.create(code='SAVE',kind='fixed',value=100,max_uses=1,per_customer=1)
        r=self.checkout(coupon='SAVE',points=20);self.assertEqual(r.status_code,200,r.content)
        o=Order.objects.get(tracking_id=r.json()['tracking_id']);self.customer.refresh_from_db();self.assertEqual(self.customer.loyalty_points,30)
        Order.objects.filter(pk=o.pk).update(reservation_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(expire_due_reservations(),1);self.assertEqual(expire_due_reservations(),0)
        self.customer.refresh_from_db();self.assertEqual(self.customer.loyalty_points,50)
        self.assertTrue(CouponUse.objects.get(order=o).released)
        self.assertEqual(self.checkout(coupon='SAVE').status_code,200)

    def test_loyalty_earned_on_delivery_reversed_on_return(self):
        p=StoreProfile.current();p.loyalty_enabled=True;p.loyalty_earn_step=100;p.save()
        r=self.checkout();o=Order.objects.get(tracking_id=r.json()['tracking_id'])
        for status in ['Confirmed','Packed','Shipped','Delivered']: transition_order(o.pk,status,self.owner)
        self.customer.refresh_from_db();self.assertEqual(self.customer.loyalty_points,11)
        process_return(o.pk,None,'Return',True,'later','return',self.owner)
        self.customer.refresh_from_db();self.assertEqual(self.customer.loyalty_points,0)

    def test_order_history_receipts_and_statements_are_private(self):
        r=self.checkout();o=Order.objects.get(tracking_id=r.json()['tracking_id'])
        self.assertEqual(len(self.client.get('/api/account/').json()['orders']),1)
        self.assertEqual(self.client.get(f'/receipt/{o.pk}/').status_code,200)
        self.assertEqual(self.client.get(f'/customer/{self.customer.pk}/statement/').status_code,200)
        other=User.objects.create_user('other');self.client.force_login(other)
        self.assertEqual(self.client.get(f'/receipt/{o.pk}/').status_code,403)
        self.assertEqual(self.client.get(f'/customer/{self.customer.pk}/statement/').status_code,403)

    def test_verified_review_requires_delivered_purchase(self):
        r=self.checkout();o=Order.objects.get(tracking_id=r.json()['tracking_id'])
        for status in ['Confirmed','Packed','Shipped','Delivered']:transition_order(o.pk,status,self.owner)
        review={'product':self.product.pk,'rating':5,'author':'Buyer','title':'Good','body':'Works well'}
        self.assertEqual(self.post('/api/reviews/',review).status_code,200)
        self.assertTrue(Review.objects.get().verified_purchase)
        second=Client();second.force_login(self.user)
        self.assertEqual(self.post('/api/reviews/',review,second).status_code,400)

    def test_crm_requires_manager_and_zone_must_be_selected(self):
        self.assertEqual(self.post('/api/crm/action/',{'action':'loyalty','enabled':True,'earn_step':100}).status_code,403)
        ShippingZone.objects.create(name='Local',charge=60)
        self.assertEqual(self.checkout().status_code,400)
        self.assertFalse(Order.objects.exists())
