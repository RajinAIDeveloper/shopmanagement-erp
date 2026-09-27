"""Catalog maintenance, settlement posting, and staff audit access."""
import json
import uuid
from decimal import Decimal
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F, Q
from django.forms.models import model_to_dict
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from .models import Product, Supplier, Order, Purchase, Settlement, AuditLog, JournalEntry
from .services import money, integer, movement, journal
from .commerce import text, flag, uploaded_image
from .selling import ACTIVE_SALE_STATUSES, current_shift, credit_entry
from .inventory import add, default_location


def permitted(user, roles):
    return user.is_authenticated and user.is_staff and (user.is_superuser or user.groups.filter(name__in=roles).exists())


def inventory_access(user): return permitted(user,['Owner','Manager','Inventory Manager'])
def finance_access(user): return permitted(user,['Owner','Manager','Accountant'])


def catalog_snapshot(p):
    return {key:getattr(p,key) for key in ['name','sku','barcode','category','brand','variant','photo','active','supplier_id','reorder_level','attributes','vat_exempt']}|{'price':str(p.price),'wholesale_price':str(p.wholesale_price) if p.wholesale_price is not None else None,'min_price':str(p.min_price) if p.min_price is not None else None}


@transaction.atomic
def save_product(d,user):
    editing=bool(d.get('id'))
    p=Product.objects.select_for_update().get(pk=integer(d['id'])) if editing else Product()
    before=catalog_snapshot(p) if editing else None
    if editing:
        if 'stock' in d and integer(d['stock'],0)!=p.stock: raise ValueError('Use a reasoned stock adjustment to change on-hand quantity.')
        if 'cost' in d and money(d['cost'])!=p.cost: raise ValueError('Average cost is maintained through purchasing; it cannot be overwritten here.')
        if d.get('category',p.category)!=p.category and p.attributes:
            if not flag(d,'clear_attributes',False): raise ValueError('Changing category requires clearing the existing specifications.')
            p.attributes={}
    for key,limit in [('name',180),('sku',60),('barcode',80),('category',80),('brand',80),('variant',100)]:
        setattr(p,key,text(d,key,limit,key in ['name','sku','barcode','category']))
    p.price=money(d.get('price'))
    p.vat_exempt=flag(d,'vat_exempt',False)
    p.wholesale_price=money(d['wholesale_price']) if d.get('wholesale_price') not in [None,''] else None
    p.min_price=money(d['min_price']) if d.get('min_price') not in [None,''] else None
    if p.min_price is not None and p.min_price>p.price: raise ValueError('Minimum price cannot exceed the selling price.')
    if p.wholesale_price is not None and p.min_price is not None and p.wholesale_price<p.min_price: raise ValueError('Wholesale price cannot be below the minimum price.')
    p.reorder_level=integer(d.get('reorder_level',10),0)
    p.active=flag(d,'active',True)
    p.photo=uploaded_image(text(d,'photo',400))
    p.supplier=Supplier.objects.get(pk=integer(d['supplier'])) if d.get('supplier') else None
    opening=0
    if not editing:
        p.cost=money(d.get('cost'))
        opening=integer(d.get('stock',0),0)
        p.stock=0
    p.save()
    if not editing:
        place=default_location('pos')
        if d.get('location'):
            from .inventory import location_for
            place=location_for(d['location'])
        if opening: add(p,place,opening,'Opening stock',p.sku,user)
        else: from .inventory import level_for; level_for(p,place)
        journal(p.sku,'Inventory','Opening equity',p.cost*opening)
    AuditLog.objects.create(staff=user,action=f'Product {"updated" if editing else "created"}: {p.sku}',detail=json.dumps({'product_id':p.pk,'before':before,'after':catalog_snapshot(p)},ensure_ascii=False))
    return p


@require_POST
def product_save(request):
    if not inventory_access(request.user): return JsonResponse({'error':'Inventory management permission required.'},status=403)
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        p=save_product(d,request.user)
        return JsonResponse({'ok':True,'id':p.pk})
    except (ValueError,TypeError,IntegrityError,Product.DoesNotExist,Supplier.DoesNotExist) as exc:
        return JsonResponse({'error':'SKU and barcode must be unique.' if isinstance(exc,IntegrityError) else str(exc)},status=400)


def settlement_data(s):
    return {'id':s.pk,'kind':s.kind,'order':s.order_id,'purchase':s.purchase_id,'amount':str(s.amount),'method':s.method,'reference':s.reference,'note':s.note,'staff':s.staff.get_username(),'date':s.created_at.isoformat(),'reverses':s.reverses_id,'reversed':hasattr(s,'reversal')}


@require_GET
@never_cache
def settlements(request):
    if not finance_access(request.user): return JsonResponse({'error':'Accounting permission required.'},status=403)
    entries=Settlement.objects.select_related('staff','reversal').order_by('-pk')[:200]
    orders=Order.objects.filter(status__in=ACTIVE_SALE_STATUSES,paid_total__lt=F('total')-F('returned_total')).select_related('customer')
    purchases=[p for p in Purchase.objects.filter(status__in=['Partially received','Received']).select_related('supplier').prefetch_related('lines__product') if p.payable-p.paid_total>0]
    refunds=Order.objects.filter(refunded_total__lt=F('paid_total')-(F('total')-F('returned_total'))).select_related('customer')
    return JsonResponse({'entries':[settlement_data(s) for s in entries],'receivables':[{'id':o.pk,'number':o.number,'channel':o.channel,'customer':o.customer.name if o.customer else 'Walk-in customer','total':str(o.net_total),'paid':str(o.paid_total),'due':str(o.due)} for o in orders],'payables':[{'id':p.pk,'number':p.number,'supplier':p.supplier.name,'product':', '.join(l.product.name for l in p.lines.all()),'total':str(p.payable),'paid':str(p.paid_total),'due':str(p.payable-p.paid_total)} for p in purchases],'refunds':[{'id':o.pk,'number':o.number,'channel':o.channel,'customer':o.customer.name if o.customer else 'Walk-in customer','customer_id':o.customer_id,'paid':str(o.paid_total),'refunded':str(o.refunded_total),'due':str(o.refund_due)} for o in refunds]})


def cash_account(method): return 'Cash' if method=='Cash' else 'Bank / mobile payments'


@transaction.atomic
def record_settlement(d,user):
    try: token=uuid.UUID(str(d.get('request_id')))
    except (ValueError,TypeError): raise ValueError('A valid payment request ID is required.')
    kind=d.get('kind')
    if kind not in ['collection','supplier_payment','refund','reversal']: raise ValueError('Unknown settlement type.')
    previous=Settlement.objects.filter(request_id=token).first()
    if kind=='reversal':
        original=Settlement.objects.select_for_update().get(pk=integer(d.get('reverses')))
        if previous:
            if previous.kind!=kind or previous.reverses_id!=original.pk: raise ValueError('Request ID already used for another settlement.')
            return previous
        if original.kind=='reversal' or Settlement.objects.filter(reverses=original).exists(): raise ValueError('This payment has already been reversed or is itself a reversal.')
        reason=text(d,'note',500,True)
        order=Order.objects.select_for_update().get(pk=original.order_id) if original.order_id else None
        purchase=Purchase.objects.select_for_update().get(pk=original.purchase_id) if original.purchase_id else None
        amount=original.amount
        account=cash_account(original.method)
        if original.kind=='collection':
            if order.returned_total>0: raise ValueError('A collection cannot be reversed after goods were returned. Record the customer refund instead.')
            if order.paid_total<amount: raise ValueError('Payment balance no longer supports this reversal.')
            order.paid_total-=amount
            order.save(update_fields=['paid_total'])
            debit,credit='Accounts receivable',account
        elif original.kind=='supplier_payment':
            if purchase.paid_total<amount: raise ValueError('Payment balance no longer supports this reversal.')
            purchase.paid_total-=amount
            purchase.save(update_fields=['paid_total'])
            debit,credit=account,'Accounts payable'
        else:
            if order.refunded_total<amount: raise ValueError('Refund balance no longer supports this reversal.')
            order.refunded_total-=amount
            order.save(update_fields=['refunded_total'])
            if original.method=='Store credit':
                credit_entry(order.customer,'reversal',-amount,f'Reversal of {original.reference}',user,order)
                account='Store credit liability'
            debit,credit=account,'Customer refunds payable'
        s=Settlement.objects.create(kind='reversal',order=order,purchase=purchase,amount=amount,method=original.method,reference=f'Reversal of payment #{original.pk}',note=reason,request_id=token,reverses=original,staff=user)
    else:
        amount=money(d.get('amount'))
        if not amount: raise ValueError('Amount must be greater than zero.')
        method=d.get('method')
        if method not in ['Cash','Card','bKash','Nagad','Bank']+(['Store credit'] if kind=='refund' else []): raise ValueError('Choose a valid payment method.')
        reference=text(d,'reference',150,True)
        note=text(d,'note',500)
        target=integer(d.get('target'))
        if previous:
            if (previous.kind,previous.order_id if kind!='supplier_payment' else previous.purchase_id,previous.amount,previous.method,previous.reference,previous.note)!=(kind,target,amount,method,reference,note): raise ValueError('Request ID already used for another settlement.')
            return previous
        order=None
        purchase=None
        account=cash_account(method)
        if kind=='supplier_payment':
            purchase=Purchase.objects.select_for_update().get(pk=target)
            if purchase.status not in ['Partially received','Received']: raise ValueError('Receive the purchase before recording payment.')
            if amount>purchase.payable-purchase.paid_total: raise ValueError('Payment exceeds the supplier amount due.')
            Purchase.objects.filter(pk=target).update(paid_total=F('paid_total')+amount)
            debit,credit='Accounts payable',account
        else:
            order=Order.objects.select_for_update().get(pk=target)
            if kind=='collection':
                if order.status not in ACTIVE_SALE_STATUSES: raise ValueError('Collections are allowed on completed, shipped or delivered sales.')
                if amount>order.due: raise ValueError('Collection exceeds the customer amount due.')
                if not Order.objects.filter(pk=target,paid_total__lte=F('total')-F('returned_total')-amount).update(paid_total=F('paid_total')+amount): raise ValueError('The receivable changed. Refresh and try again.')
                debit,credit=account,'Accounts receivable'
            else:
                if order.refund_due<=0: raise ValueError('Return the goods before recording a refund.')
                if amount>order.refund_due: raise ValueError('Refund exceeds the amount received and not yet refunded.')
                if not Order.objects.filter(pk=target,refunded_total__lte=F('paid_total')-(F('total')-F('returned_total'))-amount).update(refunded_total=F('refunded_total')+amount): raise ValueError('The refund balance changed. Refresh and try again.')
                if method=='Store credit':
                    if order.customer is None: raise ValueError('Store credit needs a customer account.')
                    credit_entry(order.customer,'refund',amount,reference,user,order)
                    account='Store credit liability'
                debit,credit='Customer refunds payable',account
        shift=current_shift() if method=='Cash' and kind!='supplier_payment' else None
        s=Settlement.objects.create(kind=kind,order=order,purchase=purchase,amount=amount,method=method,reference=reference,note=note,request_id=token,staff=user,shift=shift)
    journal(f'PAY-{s.pk:05d} · {s.reference}',debit,credit,s.amount)
    AuditLog.objects.create(staff=user,action=f'{s.get_kind_display()}: PAY-{s.pk:05d}',detail=json.dumps({'order':s.order_id,'purchase':s.purchase_id,'amount':str(s.amount),'method':s.method,'reference':s.reference,'note':s.note,'reverses':s.reverses_id}))
    return s


@require_POST
def settlement_save(request):
    if not finance_access(request.user): return JsonResponse({'error':'Accounting permission required.'},status=403)
    try:
        d=json.loads(request.body)
        if not isinstance(d,dict): raise ValueError('Expected an object.')
        s=record_settlement(d,request.user)
        return JsonResponse({'ok':True,'id':s.pk})
    except (ValueError,TypeError,Order.DoesNotExist,Purchase.DoesNotExist,Settlement.DoesNotExist) as exc:
        return JsonResponse({'error':str(exc)},status=400)
    except (IntegrityError,OperationalError):
        return JsonResponse({'error':'Another payment updated this record. Refresh or retry this same request.'},status=409)


@require_GET
@never_cache
def activity(request):
    if not finance_access(request.user): return JsonResponse({'error':'Audit permission required.'},status=403)
    query=request.GET.get('q','')[:200]
    logs=AuditLog.objects.select_related('staff').order_by('-pk')
    if query: logs=logs.filter(Q(action__icontains=query)|Q(detail__icontains=query)|Q(staff__username__icontains=query))
    if request.GET.get('before'):
        try: logs=logs.filter(pk__lt=integer(request.GET['before']))
        except ValueError: return JsonResponse({'error':'Invalid cursor.'},status=400)
    rows=list(logs[:51])
    return JsonResponse({'entries':[{'id':a.pk,'action':a.action,'detail':a.detail,'staff':a.staff.get_username() if a.staff else 'System / online customer','date':a.created_at.isoformat()} for a in rows[:50]],'next':rows[49].pk if len(rows)>50 else None})
