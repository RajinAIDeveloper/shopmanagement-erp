import csv
import json
import re
import uuid
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST, require_GET
from .models import Product, Supplier, Customer, Order, Purchase, StockMovement, Expense, JournalEntry, AuditLog, Review
from .services import create_order, transition_order, money, integer, movement, journal, sweep_reservations, display_status
from .commerce import commerce_data, review_summary, can_manage
from .pricing import active_offers, offer_price
from .operations import inventory_access, finance_access, save_product
from .security import throttled, throttled_response
from .selling import current_shift, shift_summary, return_data, save_customer, adjust_credit, customer_outstanding, manager_access
from .inventory import add, take, default_location, location_for, location_data, purchase_data, create_purchase, receive_purchase, return_purchase, cancel_purchase, levels_by_product, available_at
from .models import Location, StockLevel, StaffProfile
from pathlib import Path
from django.conf import settings


@ensure_csrf_cookie
@login_required
def workspace(request):
    if not request.user.is_staff: return HttpResponse('Staff access required.', status=403)
    return render(request, 'workspace.html')


@ensure_csrf_cookie
def shop(request): return render(request, 'shop.html')


def product_data(p, private=True, offers=None, ratings=None, levels=None, available=None):
    data = {'id': p.pk, 'name': p.name, 'sku': p.sku, 'barcode': p.barcode, 'category': p.category, 'brand': p.brand, 'variant': p.variant, 'price': float(p.price), 'available': p.available, 'image': p.image, 'photo': p.photo, 'active': p.active}
    if private: data.update(cost=float(p.cost), stock=p.stock, reserved=p.reserved, reorder_level=p.reorder_level, supplier=p.supplier_id, vat_exempt=p.vat_exempt, wholesale_price=float(p.wholesale_price) if p.wholesale_price is not None else None, min_price=float(p.min_price) if p.min_price is not None else None)
    data['attributes'] = p.attributes
    data['rating'] = (ratings or {}).get(p.pk, {'average':0,'count':0})
    if levels is not None: data['levels'] = levels.get(p.pk, {})
    if available is not None: data['available'] = available
    if not private:
        price, offer = offer_price(p, offers or [])
        data.update(price=float(price), original_price=float(p.price), offer_id=offer.pk if offer else None, offer_title=offer.title if offer else '')
    return data


def order_data(o):
    return {'id': o.pk, 'number': o.number, 'customer': o.customer.name if o.customer else 'Walk-in customer', 'customer_id': o.customer_id, 'channel': o.channel, 'status': display_status(o), 'payment': o.payment, 'total': float(o.total), 'discount': float(o.discount), 'tax_total': float(o.tax_total), 'date': o.created_at.isoformat(), 'tracking_id': str(o.tracking_id), 'paid_total': float(o.paid_total), 'refunded_total': float(o.refunded_total), 'returned_total': float(o.returned_total), 'net_total': float(o.net_total), 'due': float(o.due) if o.status in ['Completed', 'Partially returned', 'Shipped', 'Delivered'] else 0, 'refund_due': float(o.refund_due), 'shift': o.shift_id, 'location': o.location.name if o.location else '', 'location_id': o.location_id, 'change': float(getattr(o, 'change', 0) or 0), 'reservation_expires_at': o.reservation_expires_at.isoformat() if o.reservation_expires_at else None,
            'items': [{'line': l.pk, 'product': l.product_id, 'name': l.name or l.product.name, 'sku': l.sku or l.product.sku, 'variant': l.variant, 'quantity': l.quantity, 'returned_quantity': l.returned_quantity, 'returnable': l.returnable, 'price': float(l.price), 'tax_rate': float(l.tax_rate)} for l in o.lines.all()],
            'payments': [{'method': pm.method, 'amount': float(pm.amount), 'reference': pm.reference} for pm in o.payments.all()],
            'returns': [return_data(r) for r in o.returns.all()]}


def customer_rows():
    """Customers with their live balances; dues are summed in one pass over open sales."""
    dues = {}
    for o in Order.objects.filter(customer__isnull=False, status__in=['Completed', 'Partially returned', 'Shipped', 'Delivered']).only('customer_id', 'total', 'returned_total', 'paid_total', 'status'):
        dues[o.customer_id] = dues.get(o.customer_id, Decimal('0')) + o.due
    rows = []
    for c in Customer.objects.all():
        rows.append({'id': c.pk, 'name': c.name, 'email': c.email, 'phone': c.phone, 'address': c.address, 'pricing_tier': c.pricing_tier, 'credit_balance': float(c.credit_balance), 'credit_limit': float(c.credit_limit), 'outstanding': float(dues.get(c.pk, 0)), 'created_at': c.created_at.isoformat()})
    return rows


@require_GET
@never_cache
def state(request):
    if not request.user.is_authenticated or not request.user.is_staff: return JsonResponse({'error': 'Staff login required.'}, status=403)
    finance = request.user.is_superuser or request.user.groups.filter(name__in=['Owner', 'Manager', 'Accountant']).exists()
    sweep_reservations()
    ratings = review_summary()
    levels = {}
    for l in StockLevel.objects.all().values('product_id', 'location_id', 'quantity', 'reserved'):
        levels.setdefault(l['product_id'], {})[str(l['location_id'])] = {'quantity': l['quantity'], 'reserved': l['reserved'], 'available': l['quantity'] - l['reserved']}
    products = [product_data(p, ratings=ratings, levels=levels) for p in Product.objects.all().order_by('name')]
    pinned = StaffProfile.objects.filter(user=request.user).values_list('location_id', flat=True).first()
    if not finance:
        for p in products: p.pop('cost', None)
    return JsonResponse({**commerce_data(private=can_manage(request.user)), 'can_manage':can_manage(request.user), 'can_inventory':inventory_access(request.user), 'reviews':list(Review.objects.select_related('product').values('id','product_id','product__name','author','rating','title','body','status','created_at')) if can_manage(request.user) else [], 'user': {'name': request.user.first_name or request.user.username, 'role': 'Owner' if request.user.is_superuser else ', '.join(request.user.groups.values_list('name', flat=True)), 'finance': finance}, 'products': products, 'orders': [order_data(o) for o in Order.objects.select_related('customer', 'location').prefetch_related('lines__product', 'payments', 'returns__lines').order_by('-created_at')[:1000]], 'customers': customer_rows(), 'shift': (lambda sh: shift_summary(sh) if sh else None)(current_shift()), 'can_manage_sales': manager_access(request.user), 'suppliers': list(Supplier.objects.values()), 'purchases': [purchase_data(p, finance) for p in Purchase.objects.select_related('supplier', 'location').prefetch_related('lines__product', 'receipts__staff', 'returns').order_by('-created_at')], 'locations': [location_data(l) for l in Location.objects.all()], 'pos_location': pinned, 'movements': list(StockMovement.objects.order_by('-created_at').values('id','product__name','location__name','quantity','kind','reference','balance','created_at')[:200]), 'expenses': list(Expense.objects.order_by('-created_at').values()) if finance else [], 'journal': list(JournalEntry.objects.order_by('-created_at').values()) if finance else []})


PERMISSIONS = {'credit': ['Owner','Manager'], 'product': ['Owner','Manager','Inventory Manager'], 'adjust': ['Owner','Manager','Inventory Manager'], 'purchase': ['Owner','Manager','Inventory Manager'], 'receive': ['Owner','Manager','Inventory Manager'], 'purchase_return': ['Owner','Manager','Inventory Manager'], 'purchase_cancel': ['Owner','Manager','Inventory Manager'], 'supplier': ['Owner','Manager','Inventory Manager'], 'customer': ['Owner','Manager','Cashier'], 'sale': ['Owner','Manager','Cashier'], 'status': ['Owner','Manager'], 'expense': ['Owner','Manager','Accountant']}


@require_POST
def action(request):
    if not request.user.is_authenticated or not request.user.is_staff: return JsonResponse({'error': 'Staff login required.'}, status=403)
    try:
        data = json.loads(request.body)
        if not isinstance(data,dict): raise ValueError('Expected an object.')
        kind = data.get('action')
        sweep_reservations()
        if kind not in PERMISSIONS: raise ValueError('Unknown action.')
        if not request.user.is_superuser and not request.user.groups.filter(name__in=PERMISSIONS[kind]).exists():
            return JsonResponse({'error': 'Your role does not have permission for this action.'}, status=403)
        with transaction.atomic():
            result = perform_action(kind, data, request.user)
            AuditLog.objects.create(staff=request.user, action=kind.title(), detail=json.dumps({k:v for k,v in data.items() if k != 'action'})[:4000])
        return JsonResponse({'ok': True, **(result or {})})
    except (ValueError, KeyError, TypeError, IntegrityError, Product.DoesNotExist, Supplier.DoesNotExist, Customer.DoesNotExist, Order.DoesNotExist, Purchase.DoesNotExist) as exc:
        return JsonResponse({'error': 'A SKU or barcode already exists, or the record is invalid.' if isinstance(exc, IntegrityError) else str(exc)}, status=400)


def required(data, key):
    value = str(data.get(key, '')).strip()
    if not value or len(value) > 180: raise ValueError(f'Enter a valid {key}.')
    return value


def phone_number(data):
    """A dialable number: optional leading +, then 10–15 digits once spaces, dashes and brackets are removed."""
    value = required(data, 'phone')
    digits = re.sub(r'[\s().-]', '', value)
    if not re.fullmatch(r'\+?\d{10,15}', digits): raise ValueError('Enter a valid phone number.')
    return value[:40]


def perform_action(kind, d, user):
    if kind == 'sale':
        customer = Customer.objects.get(pk=d['customer']) if d.get('customer') else None
        exchange = None
        if d.get('exchange_return'):
            from .models import SaleReturn
            exchange = SaleReturn.objects.select_for_update().filter(pk=integer(d['exchange_return']), kind='exchange').first()
            if exchange is None: raise ValueError('That exchange is no longer open.')
        request_id = None
        if d.get('request_id'):
            try: request_id = uuid.UUID(str(d['request_id']))
            except ValueError: raise ValueError('Invalid sale request ID.')
        order = create_order(d['items'], 'POS', d.get('payment','Cash'), customer, d.get('discount',0), user, tenders=d.get('tenders'), request_id=request_id, manager=manager_access(user), exchange_return=exchange, location=d.get('location'))
        return {'order': order_data(order)}
    if kind == 'credit':
        entry = adjust_credit(d, user)
        return {'balance': float(entry.balance)}
    if kind == 'status':
        transition_order(d['id'], d['status'], user)
    elif kind == 'customer':
        c = save_customer(d, user, manager_access(user))
        return {'id':c.pk}
    elif kind == 'supplier':
        Supplier.objects.create(name=required(d,'name'), email=d.get('email','')[:254], phone=d.get('phone','')[:40])
    elif kind == 'product':
        return {'id':save_product(d,user).pk}
    elif kind == 'adjust':
        p = Product.objects.select_for_update().get(pk=d['product'])
        quantity = integer(d['quantity'], -1000000)
        reason = required(d,'reason')
        if not quantity: raise ValueError('Adjustment cannot be zero.')
        place = location_for(d['location']) if d.get('location') else default_location('pos')
        try:
            if quantity > 0: add(p, place, quantity, 'Adjustment', reason, user)
            else: take(p, place, -quantity, 'Adjustment', reason, user)
        except ValueError: raise ValueError('Adjustment would consume reserved stock or make stock negative.')
        journal(reason, 'Inventory' if quantity > 0 else 'Inventory loss', 'Inventory gain' if quantity > 0 else 'Inventory', abs(quantity)*p.cost)
    elif kind == 'purchase':
        return {'id': create_purchase(d, user).pk}
    elif kind == 'receive':
        return {'receipt': receive_purchase(d, user).number}
    elif kind == 'purchase_return':
        return {'return': return_purchase(d, user).number}
    elif kind == 'purchase_cancel':
        cancel_purchase(d, user)
    elif kind == 'expense':
        amount = money(d['amount'])
        if not amount: raise ValueError('Expense must be greater than zero.')
        e = Expense.objects.create(description=required(d,'description'), category=required(d,'category'), amount=amount)
        journal(f'Expense #{e.pk}','Operating expenses','Cash',amount)


@require_GET
@never_cache
def catalog(request):
    offers, ratings = active_offers(), review_summary()
    place = default_location('online')
    levels = levels_by_product(place)
    from .customers import wishlist_ids, storefront_data
    return JsonResponse({**commerce_data(), **storefront_data(request), 'wishlist':wishlist_ids(request), 'products': [product_data(p,False,offers,ratings,available=available_at(p, place, levels)) for p in Product.objects.filter(active=True).order_by('name')]})


@require_POST
def checkout(request):
    if throttled(request, 'checkout'): return throttled_response('Too many orders from this connection. Please wait a few minutes and try again.')
    try:
        d = json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        sweep_reservations()
        with transaction.atomic():
            from .customers import customer_for, quote, coupon_identity, loyalty_post
            from .models import CouponUse
            customer = customer_for(request)
            if customer:
                customer = Customer.objects.select_for_update().get(pk=customer.pk)
                customer.name=required(d,'name'); customer.phone=phone_number(d); customer.address=required(d,'address')
                customer.save(update_fields=['name','phone','address'])
            else:
                customer = Customer.objects.create(name=required(d,'name'), phone=phone_number(d), address=required(d,'address'), email=str(d.get('email',''))[:254])
            q=quote(d,customer,lock=True)
            order = create_order(d['items'],'Online','COD',customer,discount=q['discount'])
            order.total+=q['shipping']; order.tax_total+=q['shipping_tax']
            order.shipping_total=q['shipping']; order.shipping_tax=q['shipping_tax']; order.shipping_zone=q['zone'].name if q['zone'] else ''
            order.coupon_code=q['coupon'].code if q['coupon'] else ''; order.loyalty_redeemed=q['points']
            order.save(update_fields=['total','tax_total','shipping_total','shipping_tax','shipping_zone','coupon_code','loyalty_redeemed'])
            if q['coupon']: CouponUse.objects.create(coupon=q['coupon'],order=order,customer=customer,identity=coupon_identity(customer,d.get('phone','')))
            if q['points']: loyalty_post(customer,order,-q['points'],f'Redeemed on {order.number}',f'redeem:{order.pk}')
        return JsonResponse({'ok':True,'number':order.number,'tracking_id':str(order.tracking_id),'total':str(order.total),'reservation_expires_at':order.reservation_expires_at.isoformat() if order.reservation_expires_at else None})
    except (ValueError, KeyError, TypeError) as exc: return JsonResponse({'error':str(exc)}, status=400)


@require_GET
@never_cache
def track(request):
    if throttled(request, 'track'): return throttled_response()
    try: order = Order.objects.get(tracking_id=request.GET.get('id',''))
    except Exception: return JsonResponse({'error':'No order found with that tracking ID.'},status=404)
    return JsonResponse({'number':order.number,'status':display_status(order),'total':str(order.total),'date':order.created_at.isoformat()})


@login_required
def receipt(request,pk):
    order=get_object_or_404(Order.objects.select_related('customer').prefetch_related('lines__product'),pk=pk)
    if not request.user.is_staff and (not order.customer_id or order.customer.user_id!=request.user.pk): return HttpResponse(status=403)
    return render(request,'receipt.html',{'order':order})


class _Line:
    def write(self, value): return value


def export_rows():
    writer = csv.writer(_Line())
    yield writer.writerow(['Order','Date','Channel','Status','Payment','Customer','Items','Total BDT','Paid BDT','Refunded BDT','Due BDT'])
    orders = Order.objects.select_related('customer').prefetch_related('lines').order_by('-created_at')
    for o in orders.iterator(chunk_size=500):
        due = o.total-o.paid_total if o.status in ['Shipped','Delivered'] else Decimal('0')
        yield writer.writerow([o.number,o.created_at.isoformat(),o.channel,display_status(o),o.payment,o.customer.name if o.customer else 'Walk-in customer',sum(l.quantity for l in o.lines.all()),f'{o.total:.2f}',f'{o.paid_total:.2f}',f'{o.refunded_total:.2f}',f'{due:.2f}'])


@login_required
def export(request):
    if not request.user.is_staff: return HttpResponse(status=403)
    response = StreamingHttpResponse(export_rows(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="altab-tech-sales.csv"'
    return response


@never_cache
def service_worker(request):
    """Serve the POS service worker from the site root so it can control every page."""
    response = HttpResponse((Path(settings.BASE_DIR) / 'static' / 'sw.js').read_text(encoding='utf-8'), content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response
