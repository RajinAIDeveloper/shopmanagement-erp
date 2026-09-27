"""Customer identities, promotions, shipping quotations and loyalty lifecycle."""
import hashlib
import json
import re
import uuid
from decimal import Decimal
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.validators import validate_email
from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from .models import Customer, Coupon, CouponUse, LoyaltyEntry, WishlistItem, ShippingZone, StoreProfile, Product, Order, AuditLog
from .commerce import text, flag, schedule
from .services import integer, money
from .selling import manager_access, sale_access, finance_access, parse_items, resolve_prices, sale_pricing, line_tax
from .security import throttled, throttled_response, throttle_hit, throttle_count


def customer_for(request):
    if not request.user.is_authenticated: return None
    return Customer.objects.filter(user=request.user).first()


def wishlist_ids(request):
    customer = customer_for(request)
    if customer: return list(WishlistItem.objects.filter(customer=customer,product__active=True).values_list('product_id',flat=True))
    return list(Product.objects.filter(pk__in=request.session.get('wishlist',[]),active=True).values_list('pk',flat=True))


def public_customer(c):
    return {'id':c.pk, 'name':c.name, 'email':c.email, 'phone':c.phone, 'address':c.address, 'birthday':c.birthday, 'loyalty_points':c.loyalty_points, 'credit_balance':c.credit_balance, 'pricing_tier':c.pricing_tier} if c else None


def storefront_data(request):
    p = StoreProfile.current()
    return {'account': public_customer(customer_for(request)), 'shipping_zones':list(ShippingZone.objects.filter(active=True).values('id','name','charge','free_above')), 'loyalty':{'enabled':p.loyalty_enabled,'earn_step':p.loyalty_earn_step,'point_value':'1.00'}}


@require_GET
@never_cache
def account(request):
    c = customer_for(request)
    if not c: return JsonResponse({'account':None,'orders':[]})
    orders = c.order_set.prefetch_related('lines').order_by('-pk')[:100]
    return JsonResponse({'account':public_customer(c), 'orders':[{'id':o.pk,'number':o.number,'status':o.status,'date':o.created_at,'total':o.total,'due':o.due,'tracking_id':str(o.tracking_id),'items':[{'name':l.name,'quantity':l.quantity,'price':l.price} for l in o.lines.all()]} for o in orders], 'loyalty_history':list(c.loyalty_entries.order_by('-pk').values('points','reason','created_at')[:100])})


@require_POST
def account_action(request):
    try:
        d = json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        kind=d.get('action')
        if kind in ['register','login']:
            if throttled(request,'customer_auth'): return throttled_response()
            if request.user.is_authenticated: raise ValueError('Sign out before using another account.')
            email=text(d,'email',150,True).lower(); validate_email(email)
            password=text(d,'password',128,True)
            saved=request.session.get('wishlist',[])
            if kind=='register':
                with transaction.atomic():
                    user=User(username=email,email=email)
                    validate_password(password,user)
                    user.set_password(password);user.save()
                    c=Customer.objects.create(user=user,name=text(d,'name',150,True),email=email)
            else:
                user=authenticate(request,username=email,password=password)
                if user is None or user.is_staff or not Customer.objects.filter(user=user).exists(): raise ValueError('Email or password is incorrect.')
                c=Customer.objects.get(user=user)
            login(request,user)
            for pk in Product.objects.filter(pk__in=saved,active=True).values_list('pk',flat=True): WishlistItem.objects.get_or_create(customer=c,product_id=pk)
        elif kind=='logout':
            logout(request)
        elif kind=='profile':
            c=customer_for(request)
            if not c: return JsonResponse({'error':'Sign in first.'},status=401)
            c.name=text(d,'name',150,True);c.phone=text(d,'phone',40);c.address=text(d,'address',1000)
            if d.get('birthday'):
                from .accounting import parsed_date
                c.birthday=parsed_date(d['birthday'])
                if c.birthday>timezone.localdate(): raise ValueError('Birthday cannot be in the future.')
            else: c.birthday=None
            c.save(update_fields=['name','phone','address','birthday'])
        else: raise ValueError('Unknown account action.')
        return JsonResponse({'ok':True})
    except ValidationError as exc: return JsonResponse({'error':' '.join(exc.messages)},status=400)
    except IntegrityError: return JsonResponse({'error':'Unable to register this email. Try signing in.'},status=400)
    except (ValueError,TypeError) as exc: return JsonResponse({'error':str(exc)},status=400)


def coupon_identity(customer,phone=''):
    return hashlib.sha256((f'user:{customer.user_id}' if customer and customer.user_id else 'phone:'+re.sub(r'\D','',phone)).encode()).hexdigest()


def quote(d, customer=None, lock=False):
    profile=StoreProfile.current()
    quantities,_=parse_items(d.get('items'))
    products={p.pk:p for p in Product.objects.filter(pk__in=quantities,active=True)}
    if len(products)!=len(quantities): raise ValueError('A product is unavailable.')
    prices=resolve_prices(products,{},customer,'Online',False)
    subtotal=sum(prices[pk]*qty for pk,qty in quantities.items())
    discount=Decimal(0); coupon=None
    code=text(d,'coupon',40).upper()
    if code:
        coupons=Coupon.objects.select_for_update() if lock else Coupon.objects
        coupon=coupons.filter(code=code,active=True).first(); now=timezone.now()
        if not coupon or coupon.starts_at and coupon.starts_at>now or coupon.ends_at and coupon.ends_at<=now: raise ValueError('This coupon is unavailable or expired.')
        if coupon.members_only and not (customer and customer.user_id): raise ValueError('Sign in to use this coupon.')
        if subtotal<coupon.minimum: raise ValueError(f'This coupon needs a minimum basket of {coupon.minimum}.')
        uses=coupon.uses.filter(released=False)
        if uses.count()>=coupon.max_uses: raise ValueError('This coupon has reached its usage limit.')
        identity=coupon_identity(customer,d.get('phone',''))
        if uses.filter(identity=identity).count()>=coupon.per_customer: raise ValueError('You have already used this coupon the allowed number of times.')
        discount=min(subtotal,money(subtotal*coupon.value/100 if coupon.kind=='percent' else coupon.value))
    points=integer(d.get('points') or 0,0)
    if points:
        if not customer or not customer.user_id or not profile.loyalty_enabled: raise ValueError('Sign in to redeem loyalty points.')
        if points>customer.loyalty_points: raise ValueError('You do not have enough loyalty points.')
        if Decimal(points)>subtotal-discount: raise ValueError('Points exceed the remaining product total.')
        discount+=Decimal(points)
    total,tax,_=sale_pricing(products,quantities,prices,discount,profile)
    zone=None; charge=Decimal(0)
    if ShippingZone.objects.filter(active=True).exists():
        zone=ShippingZone.objects.filter(pk=integer(d.get('zone')),active=True).first()
        if not zone: raise ValueError('Choose a delivery zone.')
        charge=zone.charge if zone.free_above is None or total<zone.free_above else Decimal(0)
    shipping_tax=line_tax(charge,profile.vat_rate,profile.prices_include_vat)
    shipping_total=charge if profile.prices_include_vat else charge+shipping_tax
    return {'subtotal':subtotal,'discount':discount,'products_total':total,'tax':tax+shipping_tax,'shipping':shipping_total,'shipping_tax':shipping_tax,'total':total+shipping_total,'coupon':coupon,'zone':zone,'points':points}


@require_POST
def checkout_quote(request):
    if throttled(request,'quote'): return throttled_response()
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        q=quote(d,customer_for(request))
        return JsonResponse({k:v for k,v in q.items() if k not in ['coupon','zone']})
    except (ValueError,TypeError,KeyError) as exc: return JsonResponse({'error':str(exc)},status=400)


@transaction.atomic
def loyalty_post(customer,order,points,reason,event):
    if not points or LoyaltyEntry.objects.filter(event=event).exists(): return
    customer=Customer.objects.select_for_update().get(pk=customer.pk)
    customer.loyalty_points+=points; customer.save(update_fields=['loyalty_points'])
    LoyaltyEntry.objects.create(customer=customer,order=order,points=points,reason=reason,event=event)


def loyalty_sync(order):
    if not order.customer_id: return
    profile=StoreProfile.current()
    if not profile.loyalty_enabled: return
    earned=LoyaltyEntry.objects.filter(order=order,event__startswith=f'earn:{order.pk}:').aggregate(total=Sum('points'))['total'] or 0
    desired=int(max(Decimal(0),order.total-order.shipping_total-order.returned_total)/profile.loyalty_earn_step) if order.status in ['Completed','Delivered','Partially returned'] else 0
    if desired!=earned:
        loyalty_post(order.customer,order,desired-earned,f'Points for {order.number}',f'earn:{order.pk}:{order.status}:{order.returned_total}')


def release_promotions(order):
    CouponUse.objects.filter(order=order,released=False).update(released=True)
    if order.customer_id and order.loyalty_redeemed:
        loyalty_post(order.customer,order,order.loyalty_redeemed,f'Cancelled order {order.number}',f'restore:{order.pk}')


@require_GET
@never_cache
def manage_data(request):
    if not manager_access(request.user): return JsonResponse({'error':'Manager access required.'},status=403)
    return JsonResponse({'coupons':list(Coupon.objects.values()),'zones':list(ShippingZone.objects.values()),'loyalty':{'enabled':StoreProfile.current().loyalty_enabled,'earn_step':StoreProfile.current().loyalty_earn_step}})


@require_POST
def manage_action(request):
    if not manager_access(request.user): return JsonResponse({'error':'Manager access required.'},status=403)
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        with transaction.atomic():
            kind=d.get('action')
            if kind=='coupon':
                c=Coupon.objects.get(pk=integer(d['id'])) if d.get('id') else Coupon()
                c.code=text(d,'code',40,True).upper()
                if not re.fullmatch(r'[A-Z0-9_-]{2,40}',c.code): raise ValueError('Use letters, digits, underscores or hyphens in the coupon code.')
                c.kind=d.get('kind');c.value=money(d.get('value'));c.minimum=money(d.get('minimum',0))
                if c.kind not in ['fixed','percent'] or not c.value or c.kind=='percent' and c.value>100: raise ValueError('Invalid coupon discount.')
                c.max_uses=integer(d.get('max_uses'));c.per_customer=integer(d.get('per_customer'))
                c.active=flag(d);c.members_only=flag(d,'members_only',False)
                for key,value in schedule(d).items(): setattr(c,key,value)
                c.save()
            elif kind=='zone':
                z=ShippingZone.objects.get(pk=integer(d['id'])) if d.get('id') else ShippingZone()
                z.name=text(d,'name',100,True);z.charge=money(d.get('charge'));z.free_above=money(d['free_above']) if d.get('free_above') not in [None,''] else None;z.active=flag(d);z.save()
            elif kind=='loyalty':
                p=StoreProfile.current();p.loyalty_enabled=flag(d,'enabled',False);p.loyalty_earn_step=integer(d.get('earn_step'));p.save(update_fields=['loyalty_enabled','loyalty_earn_step'])
            elif kind=='crm':
                c=Customer.objects.get(pk=integer(d.get('id')));c.notes=text(d,'notes',5000);c.tags=[s.strip() for s in text(d,'tags',500).split(',') if s.strip()][:20];c.save(update_fields=['notes','tags'])
            else: raise ValueError('Unknown customer setting.')
            AuditLog.objects.create(staff=request.user,action=f'Customer settings: {kind}',detail=json.dumps(d))
        return JsonResponse({'ok':True})
    except (ValueError,TypeError,ObjectDoesNotExist,IntegrityError) as exc: return JsonResponse({'error':'A record with that code or name already exists.' if isinstance(exc,IntegrityError) else str(exc)},status=400)


@require_GET
@never_cache
def statement(request,pk):
    c=get_object_or_404(Customer,pk=pk)
    own=request.user.is_authenticated and c.user_id==request.user.pk
    if not own and not (sale_access(request.user) or finance_access(request.user)): return HttpResponse(status=403)
    orders=c.order_set.exclude(status__in=['Pending','Confirmed','Packed','Cancelled','Expired']).order_by('created_at')
    return render(request,'customer-statement.html',{'customer':c,'orders':orders,'due':sum((o.due for o in orders),Decimal(0)),'today':timezone.localdate()})
