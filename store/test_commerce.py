import io
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from PIL import Image
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .models import StoreProfile, Product, Offer, Review, CategoryAttribute, Announcement
from .services import create_order


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CommerceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner=User.objects.create_user('manager',password='test',is_staff=True,is_superuser=True)
        self.client.force_login(self.owner)
        self.product=Product.objects.create(name='Monitor',sku='MON',barcode='MON123',category='Displays',price=1000,cost=500,stock=30)

    def post(self,path,data,client=None):
        return (client or self.client).post(path,json.dumps(data),content_type='application/json')

    def content(self,kind,**data):
        return self.post('/api/content/',{'kind':kind,**data})

    def test_profile_persists_and_updates_public_and_receipt_branding(self):
        profile=self.client.get('/api/state/').json()['profile']
        profile.update(name='New Store',theme='dark',primary_color='#2563eb',accent_color='#dbeafe',email='shop@example.com',phone='01700000000')
        self.assertEqual(self.content('profile',**profile).status_code,200)
        self.assertEqual(Client().get('/api/catalog/').json()['profile']['name'],'New Store')
        self.assertContains(self.client.get('/login/'),'New Store')
        order=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash')
        self.assertContains(self.client.get(f'/receipt/{order.pk}/'),'New Store')

    def test_profile_rejects_css_and_script_injection(self):
        profile=self.client.get('/api/state/').json()['profile']
        profile.update(primary_color='red;bad')
        self.assertEqual(self.content('profile',**profile).status_code,400)
        self.assertEqual(StoreProfile.current().primary_color,'#176b50')
        self.assertEqual(self.content('announcement',message='Bad link',link='javascript:alert(1)').status_code,400)
        self.assertEqual(self.content('announcement',message='Bad link',link='//evil.test').status_code,400)

    def test_only_managers_can_modify_content(self):
        cashier=User.objects.create_user('cashier',is_staff=True)
        cashier.groups.add(Group.objects.create(name='Cashier'))
        self.client.force_login(cashier)
        self.assertEqual(self.content('announcement',message='Test').status_code,403)
        self.assertFalse(self.client.get('/api/state/').json()['can_manage'])
        self.client.logout()
        self.assertEqual(self.content('offer',title='Test',value=10).status_code,403)

    def test_upload_accepts_real_images_and_rejects_svg(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            output=io.BytesIO()
            Image.new('RGB',(20,20),'red').save(output,format='PNG')
            response=self.client.post('/api/upload/',{'image':SimpleUploadedFile('logo.png',output.getvalue(),content_type='image/png')})
            self.assertEqual(response.status_code,200)
            profile=self.client.get('/api/state/').json()['profile']
            profile['logo']=response.json()['url']
            self.assertEqual(self.content('profile',**profile).status_code,200)
            self.assertEqual(self.client.post('/api/upload/',{'image':SimpleUploadedFile('logo.svg',b'<svg onload="alert(1)"/>')}).status_code,400)
            self.assertEqual(self.client.post('/api/upload/',{'image':SimpleUploadedFile('fake.png',b'not an image')}).status_code,400)

    def test_offers_apply_best_discount_online_only_and_preserve_sale_price(self):
        Offer.objects.create(title='10 percent',discount_type='percent',value=10,category='Displays')
        Offer.objects.create(title='Better fixed deal',discount_type='fixed',value=150,product=self.product)
        Offer.objects.create(title='Wrong category',discount_type='percent',value=90,category='Laptops')
        Offer.objects.create(title='Future',discount_type='percent',value=99,starts_at=timezone.now()+timedelta(days=1))
        Offer.objects.create(title='Expired',discount_type='percent',value=99,ends_at=timezone.now()-timedelta(days=1))
        Offer.objects.create(title='Disabled',discount_type='percent',value=99,active=False)
        catalog=self.client.get('/api/catalog/').json()
        self.assertEqual(catalog['products'][0]['price'],850)
        self.assertEqual(catalog['products'][0]['original_price'],1000)
        self.assertEqual(len(catalog['offers']),3)
        self.assertEqual(next(o for o in catalog['offers'] if o['title']=='Better fixed deal')['product'],self.product.pk)
        online=create_order([{'product':self.product.pk,'quantity':2}],'Online','COD')
        pos=create_order([{'product':self.product.pk,'quantity':1}],'POS','Cash')
        self.assertEqual(online.total,1700)
        self.assertEqual(pos.total,1000)
        Offer.objects.all().update(active=False)
        self.assertEqual(online.lines.get().price,Decimal('850'))

    def test_offer_validation_and_schedules(self):
        self.assertEqual(self.content('offer',title='Bad',discount_type='percent',value=101).status_code,400)
        self.assertEqual(self.content('offer',title='Bad',discount_type='fixed',value=-1).status_code,400)
        self.assertEqual(self.content('offer',title='Bad',discount_type='percent',value=10,product=self.product.pk,category='Displays').status_code,400)
        self.assertEqual(self.content('announcement',message='Bad',starts_at='2026-09-20T00:00:00Z',ends_at='2026-09-19T00:00:00Z').status_code,400)

    def test_only_live_announcements_are_public_and_sorted(self):
        Announcement.objects.create(message='Second',position=2)
        Announcement.objects.create(message='First',position=1)
        Announcement.objects.create(message='Hidden',active=False)
        Announcement.objects.create(message='Future',starts_at=timezone.now()+timedelta(days=1))
        self.assertEqual([a['message'] for a in self.client.get('/api/catalog/').json()['announcements']],['First','Second'])

    def test_category_attributes_validate_and_delete_cleans_assignments(self):
        response=self.content('attribute',category='Displays',name='Refresh rate',options=['60Hz','144Hz','165Hz'])
        self.assertEqual(response.status_code,200)
        pk=response.json()['id']
        self.assertEqual(self.content('specifications',product=self.product.pk,attributes={str(pk):['144Hz']}).status_code,200)
        self.assertEqual(self.content('specifications',product=self.product.pk,attributes={str(pk):['240Hz']}).status_code,400)
        other=CategoryAttribute.objects.create(category='Laptops',name='RAM',options=['8GB'])
        self.assertEqual(self.content('specifications',product=self.product.pk,attributes={str(other.pk):['8GB']}).status_code,400)
        self.assertEqual(self.content('attribute',id=pk,category='Displays',name='Refresh rate',options=['60Hz']).status_code,400)
        self.assertEqual(self.content('delete',resource='attribute',id=pk).status_code,200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.attributes,{})

    def test_wishlist_is_persistent_idempotent_and_private_to_browser(self):
        visitor=Client()
        for _ in range(2):
            self.assertEqual(self.post('/api/wishlist/',{'product':self.product.pk,'saved':True},visitor).json()['wishlist'],[self.product.pk])
        self.assertEqual(visitor.get('/api/catalog/').json()['wishlist'],[self.product.pk])
        self.assertEqual(Client().get('/api/catalog/').json()['wishlist'],[])
        self.assertEqual(self.post('/api/wishlist/',{'product':self.product.pk,'saved':False},visitor).json()['wishlist'],[])

    def test_reviews_require_moderation_and_only_approved_ratings_count(self):
        guest=Client()
        review={'product':self.product.pk,'author':'Buyer','rating':4,'title':'Very good','body':'The display looks sharp.'}
        self.assertEqual(self.post('/api/reviews/',review,guest).status_code,200)
        self.assertEqual(guest.get(f'/api/reviews/{self.product.pk}/').json()['reviews'],[])
        self.assertEqual(guest.get('/api/catalog/').json()['products'][0]['rating']['count'],0)
        record=Review.objects.get()
        self.assertEqual(self.content('moderate',id=record.pk,status='approved').status_code,200)
        self.assertEqual(guest.get('/api/catalog/').json()['products'][0]['rating'],{'average':4.0,'count':1})
        self.assertNotIn('visitor',guest.get(f'/api/reviews/{self.product.pk}/').json()['reviews'][0])
        self.assertEqual(self.post('/api/reviews/',review,guest).status_code,400)
        self.content('moderate',id=record.pk,status='hidden')
        self.assertEqual(guest.get('/api/catalog/').json()['products'][0]['rating']['count'],0)

    def test_invalid_ratings_and_csrf_rejected(self):
        guest=Client()
        for rating in [0,6,'NaN',1.5]:
            self.assertEqual(self.post('/api/reviews/',{'product':self.product.pk,'author':'Buyer','rating':rating,'title':'Title','body':'Body'},guest).status_code,400)
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(self.post('/api/wishlist/',{'product':self.product.pk,'saved':True},client).status_code,403)
        self.assertEqual(self.post('/api/reviews/',{'product':self.product.pk},client).status_code,403)
