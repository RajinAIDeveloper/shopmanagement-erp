"""Branding, merchandising, category specifications, and customer feedback."""
import io
import json
import re
import uuid
from urllib.parse import urlparse
from PIL import Image, UnidentifiedImageError
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.validators import validate_email
from django.db import transaction, IntegrityError
from django.db.models import Avg, Count
from django.forms.models import model_to_dict
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from .models import StoreProfile, CategoryAttribute, Offer, Announcement, Review, Product, AuditLog
from .pricing import live_records
from .services import money, integer
from .security import throttled, throttled_response


def can_manage(user):
    return user.is_authenticated and user.is_staff and (user.is_superuser or user.groups.filter(name__in=['Owner','Manager']).exists())


def profile_data():
    return model_to_dict(StoreProfile.current())


def branding(request):
    profile=StoreProfile.current()
    return {'store_profile': profile, 'store_branding': model_to_dict(profile)}


def commerce_data(private=False):
    offers = Offer.objects.all() if private else live_records(Offer.objects.all())
    announcements = Announcement.objects.all() if private else live_records(Announcement.objects.all())
    offers_data=[{**offer, 'product':offer['product_id']} for offer in offers.order_by('-pk').values()]
    return {'profile': profile_data(), 'attributes': list(CategoryAttribute.objects.values()), 'offers': offers_data, 'announcements': list(announcements.values())}


def review_summary():
    return {r['product_id']: {'average':round(r['average'],1),'count':r['count']} for r in Review.objects.filter(status='approved').values('product_id').annotate(average=Avg('rating'),count=Count('id'))}


def text(d, key, limit, required=False):
    value = d.get(key, '')
    if not isinstance(value, str): raise ValueError(f'{key} must be text.')
    value = value.strip()
    if len(value)>limit or (required and not value): raise ValueError(f'{key.replace("_"," ").title()} is required and must be no more than {limit} characters.' if required else f'{key} is too long.')
    return value


def flag(d, key='active', default=True):
    value=d.get(key,default)
    if not isinstance(value,bool): raise ValueError(f'{key} must be true or false.')
    return value


def safe_link(value):
    if not value: return ''
    parsed=urlparse(value)
    if any(c.isspace() or ord(c)<32 for c in value) or '\\' in value:
        raise ValueError('Enter a valid link without spaces.')
    if value.startswith('/') and not value.startswith('//'): return value
    if parsed.scheme=='https' and parsed.netloc and not parsed.username and not parsed.password: return value
    raise ValueError('Links must start with / or https://.')


def uploaded_image(value):
    if not value: return ''
    if not re.fullmatch(r'/media/uploads/[a-f0-9]{32}\.png',value):
        raise ValueError('Choose an image using the upload control.')
    if not default_storage.exists(value.removeprefix('/media/')): raise ValueError('Uploaded image not found. Please upload it again.')
    return value


def schedule(d):
    values={}
    for key in ['starts_at','ends_at']:
        value=d.get(key)
        if not value:
            values[key]=None
        else:
            if not isinstance(value,str): raise ValueError('Enter a valid date and time.')
            dt=parse_datetime(value)
            if dt is None: raise ValueError('Enter a valid date and time.')
            values[key]=timezone.make_aware(dt) if timezone.is_naive(dt) else dt
    if values['starts_at'] and values['ends_at'] and values['ends_at']<=values['starts_at']:
        raise ValueError('End time must be after start time.')
    return values


@require_POST
def upload(request):
    if not (can_manage(request.user) or request.user.is_authenticated and request.user.is_staff and request.user.groups.filter(name='Inventory Manager').exists()): return JsonResponse({'error':'Inventory or store management permission required to upload images.'},status=403)
    if throttled(request,'upload'): return throttled_response('Upload limit reached for now. Try again in an hour.')
    file=request.FILES.get('image')
    if not file or file.size>3*1024*1024: return JsonResponse({'error':'Choose a PNG, JPEG or WebP image under 3 MB.'},status=400)
    try:
        source=Image.open(file)
        if source.format not in ['PNG','JPEG','WEBP'] or source.width*source.height>16000000:
            raise ValueError('Use a PNG, JPEG or WebP image under 16 megapixels.')
        source.load()
        source=source.convert('RGBA')
        source.thumbnail((1800,1800))
        output=io.BytesIO()
        source.save(output,format='PNG')
        name=default_storage.save(f'uploads/{uuid.uuid4().hex}.png',ContentFile(output.getvalue()))
        return JsonResponse({'url':default_storage.url(name)})
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return JsonResponse({'error':'This image could not be read. Use a valid PNG, JPEG or WebP under 16 megapixels.'},status=400)


@require_POST
def manage(request):
    if not can_manage(request.user): return JsonResponse({'error':'Only owners and managers can change store content.'},status=403)
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        kind=d.get('kind')
        with transaction.atomic():
            result=save_content(kind,d)
            AuditLog.objects.create(staff=request.user,action=f'Updated {kind}',detail=json.dumps(d)[:4000])
        return JsonResponse({'ok':True,**(result or {})})
    except (ValueError, TypeError, KeyError, ValidationError, IntegrityError) as exc:
        return JsonResponse({'error':'That category filter already exists.' if isinstance(exc,IntegrityError) else str(exc)},status=400)


def record(model, d):
    if not d.get('id'): return model()
    obj=model.objects.filter(pk=integer(d['id'])).first()
    if obj is None: raise ValueError('This record no longer exists.')
    return obj


def save_content(kind,d):
    if kind=='profile':
        p=StoreProfile.current()
        for key,limit in [('name',100),('tagline',200),('description',500),('address',300),('phone',40),('email',254)]:
            if key in d: setattr(p,key,text(d,key,limit,required=key=='name'))
        if p.email: validate_email(p.email)
        for key in ['primary_color','accent_color']:
            if key not in d: continue
            value=text(d,key,7,True)
            if not re.fullmatch(r'#[0-9a-fA-F]{6}',value): raise ValueError('Choose a valid color.')
            setattr(p,key,value)
        if 'theme' in d:
            if d.get('theme') not in ['light','dark','system']: raise ValueError('Choose light, dark, or system theme.')
            p.theme=d['theme']
        if 'logo' in d: p.logo=uploaded_image(text(d,'logo',400))
        p.announcement_speed=integer(d.get('announcement_speed',p.announcement_speed))
        if not 10<=p.announcement_speed<=120: raise ValueError('Announcement speed must be 10–120 seconds.')
        p.reservation_hours=integer(d.get('reservation_hours',p.reservation_hours))
        if not 1<=p.reservation_hours<=168: raise ValueError('Reservation timeout must be 1–168 hours.')
        if 'vat_rate' in d:
            p.vat_rate=money(d.get('vat_rate'))
            if p.vat_rate>100: raise ValueError('VAT rate must be between 0 and 100.')
        if 'prices_include_vat' in d: p.prices_include_vat=flag(d,'prices_include_vat',True)
        if 'require_shift' in d: p.require_shift=flag(d,'require_shift',False)
        p.vat_registration=text(d,'vat_registration',40) if 'vat_registration' in d else p.vat_registration
        p.receipt_footer=text(d,'receipt_footer',300) if 'receipt_footer' in d else p.receipt_footer
        p.save()
        return {'profile':profile_data()}
    if kind in ['offer','announcement']:
        obj=record(Offer if kind=='offer' else Announcement,d)
        if kind=='offer':
            obj.title=text(d,'title',120,True)
            obj.description=text(d,'description',600)
            obj.image=uploaded_image(text(d,'image',400))
            obj.category=text(d,'category',80)
            obj.product_id=integer(d['product']) if d.get('product') else None
            if obj.product_id and not Product.objects.filter(pk=obj.product_id,active=True).exists(): raise ValueError('Choose an existing product.')
            if obj.product_id and obj.category: raise ValueError('Choose either a product or a category target.')
            if obj.category and not Product.objects.filter(category=obj.category,active=True).exists(): raise ValueError('Choose an existing category.')
            obj.discount_type=d.get('discount_type')
            if obj.discount_type not in ['percent','fixed']: raise ValueError('Choose a valid discount type.')
            obj.value=money(d.get('value'))
            if obj.value<=0 or (obj.discount_type=='percent' and obj.value>100): raise ValueError('Discount must be positive; percentages cannot exceed 100.')
        else:
            obj.message=text(d,'message',300,True)
            obj.link=safe_link(text(d,'link',400))
            obj.position=integer(d.get('position',0),0)
        for key,value in schedule(d).items(): setattr(obj,key,value)
        obj.active=flag(d)
        obj.save()
        return {'id':obj.pk}
    if kind=='attribute':
        obj=record(CategoryAttribute,d)
        category=text(d,'category',80,True)
        if obj.pk and category!=obj.category: raise ValueError('Create a new filter to use another category.')
        obj.category=category
        obj.name=text(d,'name',80,True)
        choices=d.get('options')
        if not isinstance(choices,list) or not choices or len(choices)>100: raise ValueError('Add between 1 and 100 filter choices.')
        if any(not isinstance(v,str) or not v.strip() or len(v.strip())>80 for v in choices): raise ValueError('Choices must contain 1–80 characters.')
        choices=list(dict.fromkeys(v.strip() for v in choices))
        if obj.pk:
            for p in Product.objects.filter(category=category):
                if any(v not in choices for v in p.attributes.get(str(obj.pk),[])):
                    raise ValueError(f'A removed choice is still used by {p.name}. Update that product first.')
        obj.options=choices
        obj.position=integer(d.get('position',0),0)
        obj.active=flag(d)
        obj.save()
        return {'id':obj.pk}
    if kind=='specifications':
        p=Product.objects.filter(pk=integer(d.get('product'))).first()
        if not p: raise ValueError('Product not found.')
        attrs=d.get('attributes')
        if not isinstance(attrs,dict): raise ValueError('Select valid product attributes.')
        definitions={str(a.pk):a for a in CategoryAttribute.objects.filter(category=p.category)}
        cleaned={}
        for key,values in attrs.items():
            if key not in definitions or not isinstance(values,list) or any(not isinstance(v,str) or v not in definitions[key].options for v in values):
                raise ValueError('Attribute choices must belong to this product’s category.')
            if values: cleaned[key]=list(dict.fromkeys(values))
        p.attributes=cleaned
        p.save(update_fields=['attributes'])
    elif kind=='moderate':
        r=record(Review,d)
        if not r.pk or d.get('status') not in ['approved','hidden','pending']: raise ValueError('Select a valid review status.')
        r.status=d['status']
        r.save(update_fields=['status'])
    elif kind=='delete':
        models={'offer':Offer,'announcement':Announcement,'attribute':CategoryAttribute}
        if d.get('resource') not in models: raise ValueError('Unknown resource.')
        obj=record(models[d['resource']],d)
        if not obj.pk: raise ValueError('Record not found.')
        if isinstance(obj,CategoryAttribute):
            for p in Product.objects.filter(category=obj.category):
                if str(obj.pk) in p.attributes:
                    p.attributes.pop(str(obj.pk))
                    p.save(update_fields=['attributes'])
        obj.delete()
    else:
        raise ValueError('Unknown content action.')


@require_GET
@never_cache
def reviews(request,pk):
    if not Product.objects.filter(pk=pk,active=True).exists(): return JsonResponse({'error':'Product not found.'},status=404)
    return JsonResponse({'reviews':list(Review.objects.filter(product_id=pk,status='approved').values('id','author','rating','title','body','created_at','verified_purchase')[:100])})


@require_POST
def submit_review(request):
    if throttled(request,'review'): return throttled_response('Review limit reached for now. Please try again later.')
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        product=Product.objects.filter(pk=integer(d.get('product')),active=True).first()
        if not product: raise ValueError('Product not found.')
        rating=integer(d.get('rating'))
        if rating>5: raise ValueError('Rating must be between 1 and 5.')
        from .customers import customer_for
        from .models import Order
        customer=customer_for(request)
        visitor=str(uuid.uuid5(uuid.NAMESPACE_URL,f'altab-customer:{customer.pk}')) if customer else request.session.get('review_visitor') or str(uuid.uuid4())
        verified=bool(customer and Order.objects.filter(customer=customer,status__in=['Completed','Delivered','Partially returned'],lines__product=product).exists())
        request.session['review_visitor']=visitor
        if Review.objects.filter(visitor=visitor).count()>=50: raise ValueError('Review limit reached for this browser.')
        with transaction.atomic():
            Review.objects.create(product=product,author=text(d,'author',80,True),rating=rating,title=text(d,'title',120,True),body=text(d,'body',2000,True),visitor=visitor,customer=customer,verified_purchase=verified)
        return JsonResponse({'ok':True,'message':'Thank you! Your review is awaiting approval.'})
    except (ValueError,TypeError,IntegrityError) as exc:
        return JsonResponse({'error':'You have already submitted a review for this product.' if isinstance(exc,IntegrityError) else str(exc)},status=400)


@require_POST
def wishlist(request):
    if throttled(request,'wishlist'): return throttled_response()
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict) or not isinstance(d.get('saved'),bool): raise ValueError('Provide a product and saved state.')
        pk=integer(d.get('product'))
        if not Product.objects.filter(pk=pk,active=True).exists(): raise ValueError('Product not found.')
        from .customers import wishlist_ids, customer_for
        from .models import WishlistItem
        saved=wishlist_ids(request)
        if d['saved'] and pk not in saved:
            if len(saved)>=200: raise ValueError('Your wishlist is full. Remove an item first.')
            saved.append(pk)
        elif not d['saved'] and pk in saved: saved.remove(pk)
        customer=customer_for(request)
        if customer:
            if d['saved']: WishlistItem.objects.get_or_create(customer=customer,product_id=pk)
            else: WishlistItem.objects.filter(customer=customer,product_id=pk).delete()
        request.session['wishlist']=saved
        return JsonResponse({'wishlist':saved})
    except (ValueError,TypeError) as exc: return JsonResponse({'error':str(exc)},status=400)
