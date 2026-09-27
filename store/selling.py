"""Point-of-sale money handling: VAT, tenders, customer dues, store credit, returns/exchanges, and drawer shifts."""
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F, Sum, Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from .models import Product, Customer, Order, OrderLine, OrderPayment, SaleReturn, SaleReturnLine, CreditTransaction, Shift, ShiftMovement, Settlement, Expense, AuditLog, StoreProfile
from .services import money, integer, movement, journal
from .pricing import active_offers, offer_price
from .inventory import take, reserve, add, note_movement, pos_location_for, default_location, available_at, levels_by_product

CASH_ACCOUNT = {'Cash': 'Cash'}
SELL_METHODS = ['Cash', 'Card', 'bKash', 'Nagad', 'Bank']
ACTIVE_SALE_STATUSES = ['Completed', 'Partially returned', 'Shipped', 'Delivered']


def cash_account(method): return 'Cash' if method == 'Cash' else 'Bank / mobile payments'


def text(d, key, limit, required=False):
    value = d.get(key, '')
    if value is None: value = ''
    if not isinstance(value, str): raise ValueError(f'{key} must be text.')
    value = value.strip()
    if len(value) > limit or (required and not value): raise ValueError(f'{key.replace("_", " ").title()} is required and must be no more than {limit} characters.')
    return value


# --- VAT -------------------------------------------------------------------------------------------------------

def line_tax(gross, rate, inclusive):
    """VAT contained in (inclusive) or added on top of (exclusive) a gross line amount."""
    if not rate: return Decimal('0')
    return gross * rate / (100 + rate) if inclusive else gross * rate / 100


def sale_pricing(products, quantities, prices, discount, profile):
    """Total, VAT and per-line rates for a sale. Order discounts reduce VAT proportionally."""
    gross = sum(prices[pk] * qty for pk, qty in quantities.items())
    if discount > gross: raise ValueError('Discount cannot exceed the order subtotal.')
    rates = {pk: (Decimal('0') if p.vat_exempt else profile.vat_rate) for pk, p in products.items()}
    tax_gross = sum(line_tax(prices[pk] * qty, rates[pk], profile.prices_include_vat) for pk, qty in quantities.items())
    factor = (gross - discount) / gross if gross else Decimal('0')
    tax_total = money(tax_gross * factor)
    total = money(gross - discount + (Decimal('0') if profile.prices_include_vat else tax_total))
    return total, tax_total, rates


# --- Customer credit and dues ---------------------------------------------------------------------------------

def customer_outstanding(customer, exclude_order=None):
    orders = Order.objects.filter(customer=customer, status__in=ACTIVE_SALE_STATUSES)
    if exclude_order: orders = orders.exclude(pk=exclude_order)
    return sum((o.due for o in orders.only('total', 'returned_total', 'paid_total', 'status')), Decimal('0'))


def credit_entry(customer, kind, amount, reference, staff=None, order=None):
    """Move store credit and return the new balance; negative amounts redeem, positive amounts grant."""
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    balance = customer.credit_balance + amount
    if balance < 0: raise ValueError(f'{customer.name} has only {customer.credit_balance:.2f} store credit available.')
    Customer.objects.filter(pk=customer.pk).update(credit_balance=balance)
    customer.credit_balance = balance
    return CreditTransaction.objects.create(customer=customer, kind=kind, amount=amount, balance=balance, reference=reference, staff=staff, order=order)


# --- Sales -------------------------------------------------------------------------------------------------------

def parse_items(items):
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ValueError('Add at least one product (maximum 100 lines).')
    quantities, overrides = {}, {}
    for item in items:
        if not isinstance(item, dict): raise ValueError('Each line needs a product and quantity.')
        pk = integer(item['product'])
        quantities[pk] = quantities.get(pk, 0) + integer(item['quantity'])
        if item.get('price') not in [None, '']:
            price = money(item['price'])
            if pk in overrides and overrides[pk] != price: raise ValueError('Use one price per product in a sale.')
            overrides[pk] = price
    return quantities, overrides


def resolve_prices(products, overrides, customer, channel, manager):
    prices = {}
    offers = active_offers() if channel == 'Online' else []
    for pk, p in products.items():
        if channel == 'Online':
            prices[pk] = offer_price(p, offers)[0]
        elif pk in overrides:
            price = overrides[pk]
            if p.min_price is not None and price < p.min_price and not manager:
                raise ValueError(f'{p.name} cannot be sold below its minimum price of {p.min_price:.2f}. Ask a manager to approve.')
            prices[pk] = price
        elif customer is not None and customer.pricing_tier == 'wholesale' and p.wholesale_price is not None:
            prices[pk] = p.wholesale_price
        else:
            prices[pk] = p.price
    return prices


def clean_tenders(tenders, total, fallback_method):
    """Normalise tender rows; cash overpayment becomes change, anything else over the total is refused."""
    if tenders is None: tenders = [{'method': fallback_method, 'amount': total}]  # legacy single-tender calls; [] means nothing tendered
    if not isinstance(tenders, list) or len(tenders) > 6: raise ValueError('Use between one and six payment lines.')
    cleaned = []
    for t in tenders:
        if not isinstance(t, dict): raise ValueError('Each payment needs a method and amount.')
        method = t.get('method')
        if method not in OrderPayment.METHODS: raise ValueError('Select a valid payment method.')
        amount = money(t.get('amount', 0))
        if amount <= 0: continue
        cleaned.append({'method': method, 'amount': amount, 'reference': text(t, 'reference', 150)})
    paid = sum(t['amount'] for t in cleaned)
    change = Decimal('0')
    if paid > total:
        change = paid - total
        cash = [t for t in cleaned if t['method'] == 'Cash']
        if not cash or cash[-1]['amount'] < change: raise ValueError('Payments exceed the total. Only cash can be tendered above the amount due.')
        cash[-1]['amount'] -= change
        cleaned = [t for t in cleaned if t['amount'] > 0]
    return cleaned, change


def record_payments(order, tenders, staff, exchange_return=None):
    """Post each tender, grant credit for anything left owing, and return (paid, change)."""
    customer = order.customer
    cleaned, change = clean_tenders(tenders, order.total, order.payment)
    paid = sum(t['amount'] for t in cleaned)
    due = order.total - paid
    if due > 0:
        if customer is None: raise ValueError('Walk-in customers must pay in full. Select a customer account to allow a balance due.')
        outstanding = customer_outstanding(customer, exclude_order=order.pk)
        if outstanding + due > customer.credit_limit:
            raise ValueError(f'{customer.name} would owe {outstanding + due:.2f}, above the credit limit of {customer.credit_limit:.2f}.')
    for t in cleaned:
        if t['method'] == 'Store credit':
            if customer is None: raise ValueError('Store credit needs a customer account.')
            credit_entry(customer, 'redeemed', -t['amount'], order.number, staff, order)
            journal(order.number, 'Store credit liability', 'Sales revenue', t['amount'])
        elif t['method'] == 'Exchange credit':
            apply_exchange_credit(order, exchange_return, t['amount'], staff)
        elif t['method'] == 'COD':
            raise ValueError('COD is for online orders.')
        else:
            journal(order.number, cash_account(t['method']), 'Sales revenue', t['amount'])
        OrderPayment.objects.create(order=order, method=t['method'], amount=t['amount'], reference=t['reference'])
    if due > 0: journal(order.number, 'Accounts receivable', 'Sales revenue', due)
    order.paid_total = paid
    order.payment = cleaned[0]['method'] if len(cleaned) == 1 else ('Split' if cleaned else 'On account')
    return paid, change


def apply_exchange_credit(order, sale_return, amount, staff):
    if sale_return is None or sale_return.kind != 'exchange': raise ValueError('Exchange credit needs an open exchange.')
    original = Order.objects.select_for_update().get(pk=sale_return.order_id)
    available = min(sale_return.total - sale_return.exchange_used, original.refund_due)
    if amount > available: raise ValueError(f'Only {available:.2f} of exchange credit remains on {original.number}.')
    Order.objects.filter(pk=original.pk).update(refunded_total=F('refunded_total') + amount)
    SaleReturn.objects.filter(pk=sale_return.pk).update(exchange_used=F('exchange_used') + amount, exchange_order=order)
    Settlement.objects.create(kind='refund', order=original, amount=amount, method='Exchange credit', reference=f'Exchanged on {order.number}', note=sale_return.number, request_id=uuid.uuid4(), staff=staff)
    journal(order.number, 'Customer refunds payable', 'Sales revenue', amount)


@transaction.atomic
def create_order(items, channel, payment, customer=None, discount=0, staff=None, tenders=None, request_id=None, manager=False, exchange_return=None, location=None):
    if request_id:
        existing = Order.objects.filter(request_id=request_id).first()
        if existing: return existing
    if payment not in OrderPayment.METHODS + ['COD']: raise ValueError('Select a valid payment method.')
    if channel == 'POS' and payment == 'COD': raise ValueError('COD is for online orders.')
    quantities, overrides = parse_items(items)
    if channel == 'Online': overrides = {}  # storefront prices are always the store's own
    products = {p.pk: p for p in Product.objects.select_for_update().filter(pk__in=quantities, active=True)}
    if len(products) != len(quantities): raise ValueError('A product is no longer available.')
    profile = StoreProfile.current()
    prices = resolve_prices(products, overrides, customer, channel, manager)
    total, tax_total, rates = sale_pricing(products, quantities, prices, money(discount), profile)
    shift = None
    if channel == 'POS':
        shift = current_shift()
        if profile.require_shift and shift is None: raise ValueError('Open a cash drawer shift before selling.')
        place = pos_location_for(staff, location)
    else:
        place = default_location('online')
    deadline = timezone.now() + timedelta(hours=profile.reservation_hours) if channel == 'Online' else None
    order = Order.objects.create(customer=customer, channel=channel, payment=payment, status='Pending' if channel == 'Online' else 'Completed', total=total, tax_total=tax_total, discount=money(discount), reservation_expires_at=deadline, staff=staff, shift=shift, location=place, request_id=request_id or None)
    cogs = Decimal('0')
    for pk, qty in quantities.items():
        p = products[pk]
        if channel == 'Online': reserve(p, place, qty, order.number, staff)
        else: take(p, place, qty, 'POS sale', order.number, staff)
        OrderLine.objects.create(order=order, product=p, quantity=qty, price=prices[pk], cost=p.cost, name=p.name, sku=p.sku, variant=p.variant, tax_rate=rates[pk])
        cogs += p.cost * qty
    if channel == 'POS':
        paid, change = record_payments(order, tenders, staff, exchange_return)
        order.save(update_fields=['paid_total', 'payment'])
        journal(order.number, 'Sales revenue', 'VAT payable', tax_total)
        journal(order.number, 'Cost of goods sold', 'Inventory', cogs)
        order.change = change
    AuditLog.objects.create(staff=staff, action=f'{channel} order created', detail=order.number)
    from .customers import loyalty_sync
    if channel == 'POS': loyalty_sync(order)
    return order


# --- Returns and exchanges ------------------------------------------------------------------------------------

def refund_now(order, amount, method, staff, note):
    """Pay a refund immediately at the counter (or move it to store credit) and clear the liability."""
    if method == 'Store credit':
        if order.customer is None: raise ValueError('Store credit needs a customer account.')
        credit_entry(order.customer, 'refund', amount, note, staff, order)
        journal(note, 'Customer refunds payable', 'Store credit liability', amount)
        shift = None
    elif method in SELL_METHODS:
        journal(note, 'Customer refunds payable', cash_account(method), amount)
        shift = current_shift() if method == 'Cash' else None
    else:
        raise ValueError('Choose how the refund is paid.')
    Order.objects.filter(pk=order.pk).update(refunded_total=F('refunded_total') + amount)
    order.refunded_total += amount
    return Settlement.objects.create(kind='refund', order=order, amount=amount, method=method, reference=note, note='Refunded at return', request_id=uuid.uuid4(), staff=staff, shift=shift)


@transaction.atomic
def process_return(order_pk, lines, reason, restock=True, refund_method='later', kind='return', staff=None):
    order = Order.objects.select_for_update().select_related('customer').get(pk=order_pk)
    if order.status not in ['Completed', 'Delivered', 'Partially returned']:
        raise ValueError('Only completed or delivered sales can be returned.')
    if kind not in ['return', 'exchange']: raise ValueError('Unknown return type.')
    if refund_method not in ['later', 'exchange', 'Store credit'] + SELL_METHODS: raise ValueError('Choose how the refund is paid.')
    if kind == 'exchange': refund_method = 'exchange'
    order_lines = {l.pk: l for l in order.lines.select_related('product').select_for_update()}
    requested = {}
    if lines is None:
        requested = {pk: l.returnable for pk, l in order_lines.items() if l.returnable > 0}
    else:
        if not isinstance(lines, list): raise ValueError('Select the items to return.')
        for item in lines:
            pk = integer(item.get('line'))
            qty = integer(item.get('quantity', 0), 0)
            if pk not in order_lines: raise ValueError('That item does not belong to this sale.')
            if qty == 0: continue
            if qty > order_lines[pk].returnable: raise ValueError(f'Only {order_lines[pk].returnable} × {order_lines[pk].name or order_lines[pk].product.name} can still be returned.')
            requested[pk] = requested.get(pk, 0) + qty
    if not requested: raise ValueError('Select at least one item to return.')
    gross = sum(l.price * l.quantity for l in order_lines.values())
    factor = (order.total-order.shipping_total) / gross if gross else Decimal('0')
    total, tax_total, cogs_restocked = Decimal('0'), Decimal('0'), Decimal('0')
    sale_return = SaleReturn.objects.create(order=order, kind=kind, reason=text({'reason': reason}, 'reason', 200, True), restock=bool(restock), total=0, refund_method=refund_method, staff=staff)
    for pk, qty in requested.items():
        line = order_lines[pk]
        amount = money(line.price * qty * factor)
        tax_total += line_tax(amount, line.tax_rate, True)
        total += amount
        OrderLine.objects.filter(pk=pk).update(returned_quantity=F('returned_quantity') + qty)
        line.returned_quantity += qty
        SaleReturnLine.objects.create(sale_return=sale_return, line=line, quantity=qty, amount=amount)
        if restock:
            add(line.product, order.location or default_location('pos'), qty, 'Return', sale_return.number, staff)
            cogs_restocked += line.cost * qty
        else:
            note_movement(line.product, order.location or default_location('pos'), 'Return (not restocked)', sale_return.number, staff)
    fully_returned = all(l.returnable == 0 for l in order_lines.values())
    if fully_returned: total = order.total - order.shipping_total - order.returned_total
    tax_total = money(min(tax_total, total))
    unpaid_before = max(Decimal('0'), order.total - order.returned_total - order.paid_total)
    to_receivable = min(total, unpaid_before)
    to_refund = total - to_receivable
    reference = f'{sale_return.number} · {order.number}'
    journal(reference, 'Sales revenue', 'Accounts receivable', to_receivable)
    journal(reference, 'Sales revenue', 'Customer refunds payable', to_refund)
    journal(reference, 'VAT payable', 'Sales revenue', tax_total)
    journal(reference, 'Inventory', 'Cost of goods sold', cogs_restocked)
    order.returned_total += total
    order.status = 'Returned' if fully_returned else 'Partially returned'
    order.reservation_expires_at = None
    order.save(update_fields=['returned_total', 'status', 'reservation_expires_at'])
    sale_return.total, sale_return.tax_total = total, tax_total
    sale_return.save(update_fields=['total', 'tax_total'])
    refund = None
    if refund_method not in ['later', 'exchange'] and to_refund > 0:
        refund = refund_now(order, to_refund, refund_method, staff, sale_return.number)
    AuditLog.objects.create(staff=staff, action=f'{order.number}: {"exchange" if kind == "exchange" else "return"} {sale_return.number}', detail=json.dumps({'lines': {str(k): v for k, v in requested.items()}, 'total': str(total), 'restock': bool(restock), 'refund_method': refund_method, 'reason': sale_return.reason}))
    from .customers import loyalty_sync, loyalty_post
    loyalty_sync(order)
    if order.customer_id and order.loyalty_redeemed:
        restored = order.customer.loyalty_entries.filter(event__startswith=f'return-points:{order.pk}:').aggregate(total=Sum('points'))['total'] or 0
        desired = order.loyalty_redeemed if fully_returned else int(order.loyalty_redeemed * order.returned_total / (order.total-order.shipping_total)) if order.total>order.shipping_total else 0
        loyalty_post(order.customer,order,desired-restored,f'Returned items {sale_return.number}',f'return-points:{order.pk}:{sale_return.pk}')
    sale_return.refund = refund
    sale_return.to_refund = to_refund
    return sale_return


# --- Drawer shifts -------------------------------------------------------------------------------------------

def current_shift():
    return Shift.objects.filter(closed_at__isnull=True).order_by('-pk').first()


def shift_summary(shift):
    end = shift.closed_at or timezone.now()
    tenders = {row['method']: row['total'] for row in OrderPayment.objects.filter(order__shift=shift).values('method').annotate(total=Sum('amount'))}
    refunds = {row['method']: row['total'] for row in Settlement.objects.filter(shift=shift, kind='refund').values('method').annotate(total=Sum('amount'))}
    collections = {row['method']: row['total'] for row in Settlement.objects.filter(shift=shift, kind='collection').values('method').annotate(total=Sum('amount'))}
    moves = {row['kind']: row['total'] for row in shift.movements.values('kind').annotate(total=Sum('amount'))}
    cash_in = tenders.get('Cash', Decimal('0')) + collections.get('Cash', Decimal('0')) + moves.get('float', Decimal('0'))
    cash_out = refunds.get('Cash', Decimal('0')) + moves.get('drop', Decimal('0')) + moves.get('payout', Decimal('0'))
    orders = shift.orders.aggregate(count=Count('id'), total=Sum('total', default=Decimal('0')))
    returns = SaleReturn.objects.filter(created_at__gte=shift.opened_at, created_at__lte=end).aggregate(count=Count('id'), total=Sum('total', default=Decimal('0')))
    expected = shift.opening_float + cash_in - cash_out
    return {
        'id': shift.pk, 'number': shift.number, 'opened_by': shift.opened_by.get_username(), 'opened_at': shift.opened_at.isoformat(),
        'closed_by': shift.closed_by.get_username() if shift.closed_by else None, 'closed_at': shift.closed_at.isoformat() if shift.closed_at else None,
        'opening_float': f'{shift.opening_float:.2f}', 'expected_cash': f'{expected:.2f}', 'counted_cash': str(shift.counted_cash) if shift.counted_cash is not None else None,
        'variance': str(shift.variance) if shift.variance is not None else None, 'note': shift.note,
        'tenders': {k: f'{v:.2f}' for k, v in tenders.items()}, 'refunds': {k: f'{v:.2f}' for k, v in refunds.items()}, 'collections': {k: f'{v:.2f}' for k, v in collections.items()},
        'movements': [{'id': m.pk, 'kind': m.kind, 'amount': str(m.amount), 'note': m.note, 'staff': m.staff.get_username() if m.staff else '', 'date': m.created_at.isoformat()} for m in shift.movements.order_by('pk')],
        'sales_count': orders['count'], 'sales_total': f"{orders['total']:.2f}", 'returns_count': returns['count'], 'returns_total': f"{returns['total']:.2f}",
    }


@transaction.atomic
def open_shift(user, opening_float, note=''):
    if Shift.objects.select_for_update().filter(closed_at__isnull=True).exists(): raise ValueError('A drawer shift is already open. Close it before opening another.')
    shift = Shift.objects.create(opened_by=user, opening_float=money(opening_float), note=note)
    AuditLog.objects.create(staff=user, action=f'Drawer opened: {shift.number}', detail=f'Opening float {shift.opening_float}')
    return shift


@transaction.atomic
def close_shift(user, counted_cash, note=''):
    shift = Shift.objects.select_for_update().filter(closed_at__isnull=True).order_by('-pk').first()
    if shift is None: raise ValueError('No drawer shift is open.')
    summary = shift_summary(shift)
    shift.expected_cash = Decimal(summary['expected_cash'])
    shift.counted_cash = money(counted_cash)
    shift.closed_by, shift.closed_at = user, timezone.now()
    if note: shift.note = (shift.note + ' · ' + note).strip(' ·')
    shift.save()
    variance = shift.counted_cash - shift.expected_cash
    if variance > 0: journal(shift.number, 'Cash', 'Cash over / short', variance)
    elif variance < 0: journal(shift.number, 'Cash over / short', 'Cash', -variance)
    AuditLog.objects.create(staff=user, action=f'Drawer closed: {shift.number}', detail=json.dumps({'expected': str(shift.expected_cash), 'counted': str(shift.counted_cash), 'variance': str(variance)}))
    return shift


@transaction.atomic
def shift_movement(user, kind, amount, note):
    shift = Shift.objects.select_for_update().filter(closed_at__isnull=True).order_by('-pk').first()
    if shift is None: raise ValueError('Open a drawer shift first.')
    if kind not in dict(ShiftMovement.KINDS): raise ValueError('Unknown drawer movement.')
    amount = money(amount)
    if amount <= 0: raise ValueError('Amount must be greater than zero.')
    note = text({'note': note}, 'note', 200, True)
    if kind in ['drop', 'payout'] and amount > Decimal(shift_summary(shift)['expected_cash']):
        raise ValueError('That is more cash than the drawer should contain.')
    move = ShiftMovement.objects.create(shift=shift, kind=kind, amount=amount, note=note, staff=user)
    reference = f'{shift.number} · {note}'
    if kind == 'drop': journal(reference, 'Bank / mobile payments', 'Cash', amount)
    elif kind == 'float': journal(reference, 'Cash', 'Bank / mobile payments', amount)
    else:
        Expense.objects.create(description=note, category='Petty cash', amount=amount)
        journal(reference, 'Operating expenses', 'Cash', amount)
    AuditLog.objects.create(staff=user, action=f'Drawer {kind}: {shift.number}', detail=f'{amount} · {note}')
    return move


# --- Serialisers and views ----------------------------------------------------------------------------------

def return_data(r):
    return {'id': r.pk, 'number': r.number, 'kind': r.kind, 'reason': r.reason, 'restock': r.restock, 'total': str(r.total), 'tax_total': str(r.tax_total), 'refund_method': r.refund_method, 'exchange_used': str(r.exchange_used), 'exchange_order': r.exchange_order_id, 'date': r.created_at.isoformat(), 'staff': r.staff.get_username() if r.staff else '', 'lines': [{'line': l.line_id, 'quantity': l.quantity, 'amount': str(l.amount)} for l in r.lines.all()]}


def permitted(user, roles):
    return user.is_authenticated and user.is_staff and (user.is_superuser or user.groups.filter(name__in=roles).exists())


def sale_access(user): return permitted(user, ['Owner', 'Manager', 'Cashier'])
def manager_access(user): return permitted(user, ['Owner', 'Manager'])
def finance_access(user): return permitted(user, ['Owner', 'Manager', 'Accountant'])


@require_POST
def returns(request):
    if not manager_access(request.user): return JsonResponse({'error': 'Owners and managers approve returns and exchanges.'}, status=403)
    try:
        d = json.loads(request.body)
        if not isinstance(d, dict): raise ValueError('Expected an object.')
        r = process_return(integer(d.get('order')), d.get('lines'), d.get('reason', ''), bool(d.get('restock', True)), d.get('refund_method', 'later'), d.get('kind', 'return'), request.user)
        original = Order.objects.get(pk=r.order_id)
        return JsonResponse({'ok': True, 'return': return_data(r), 'order_status': original.status, 'refund_due': str(original.refund_due), 'to_refund': str(r.to_refund), 'refunded_now': str(r.refund.amount) if r.refund else '0.00'})
    except (ValueError, TypeError, KeyError, Order.DoesNotExist) as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except (IntegrityError, OperationalError):
        return JsonResponse({'error': 'Another change touched this sale. Refresh and try again.'}, status=409)


@require_GET
@never_cache
def shifts(request):
    if not sale_access(request.user) and not finance_access(request.user): return JsonResponse({'error': 'Staff access required.'}, status=403)
    current = current_shift()
    history = [shift_summary(s) for s in Shift.objects.filter(closed_at__isnull=False).select_related('opened_by', 'closed_by').order_by('-pk')[:30]] if finance_access(request.user) else []
    return JsonResponse({'current': shift_summary(current) if current else None, 'history': history})


@require_POST
def shift_action(request):
    if not sale_access(request.user): return JsonResponse({'error': 'Cashier access required.'}, status=403)
    try:
        d = json.loads(request.body)
        if not isinstance(d, dict): raise ValueError('Expected an object.')
        kind = d.get('action')
        if kind == 'open': shift = open_shift(request.user, d.get('opening_float', 0), text(d, 'note', 500))
        elif kind == 'close': shift = close_shift(request.user, d.get('counted_cash', 0), text(d, 'note', 500))
        elif kind == 'movement': shift = shift_movement(request.user, d.get('kind'), d.get('amount'), d.get('note', '')).shift
        else: raise ValueError('Unknown drawer action.')
        return JsonResponse({'ok': True, 'shift': shift_summary(shift)})
    except (ValueError, TypeError, KeyError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except (IntegrityError, OperationalError):
        return JsonResponse({'error': 'The drawer changed under you. Refresh and try again.'}, status=409)


@require_GET
@never_cache
def customer_account(request, pk):
    if not sale_access(request.user) and not finance_access(request.user): return JsonResponse({'error': 'Staff access required.'}, status=403)
    try: customer = Customer.objects.get(pk=pk)
    except Customer.DoesNotExist: return JsonResponse({'error': 'Customer not found.'}, status=404)
    orders = Order.objects.filter(customer=customer, status__in=ACTIVE_SALE_STATUSES).order_by('-pk')
    return JsonResponse({
        'id': customer.pk, 'name': customer.name, 'pricing_tier': customer.pricing_tier, 'credit_balance': str(customer.credit_balance), 'credit_limit': str(customer.credit_limit),
        'outstanding': str(customer_outstanding(customer)),
        'dues': [{'id': o.pk, 'number': o.number, 'due': str(o.due), 'date': o.created_at.isoformat()} for o in orders if o.due > 0],
        'credit_history': [{'id': c.pk, 'kind': c.kind, 'amount': str(c.amount), 'balance': str(c.balance), 'reference': c.reference, 'date': c.created_at.isoformat()} for c in customer.credit_transactions.order_by('-pk')[:50]],
    })


@transaction.atomic
def save_customer(d, user, manager):
    editing = bool(d.get('id'))
    c = Customer.objects.select_for_update().get(pk=integer(d['id'])) if editing else Customer()
    c.name = text(d, 'name', 150, True)
    c.email = text(d, 'email', 254)
    c.phone = text(d, 'phone', 40)
    c.address = text(d, 'address', 1000)
    if 'pricing_tier' in d or 'credit_limit' in d:
        if not manager: raise ValueError('Only owners and managers can change pricing tiers or credit limits.')
        tier = d.get('pricing_tier', c.pricing_tier)
        if tier not in dict(Customer.TIERS): raise ValueError('Choose retail or wholesale pricing.')
        c.pricing_tier = tier
        c.credit_limit = money(d.get('credit_limit', c.credit_limit))
    c.save()
    return c


@transaction.atomic
def adjust_credit(d, user):
    customer = Customer.objects.get(pk=integer(d.get('customer')))
    amount = money(abs(Decimal(str(d.get('amount', 0)))))
    if amount <= 0: raise ValueError('Amount must be greater than zero.')
    if str(d.get('amount')).strip().startswith('-'): amount = -amount
    reason = text(d, 'reason', 150, True)
    entry = credit_entry(customer, 'adjustment', amount, reason, user)
    if amount > 0: journal(f'Store credit · {customer.name}', 'Customer goodwill', 'Store credit liability', amount)
    else: journal(f'Store credit · {customer.name}', 'Store credit liability', 'Customer goodwill', -amount)
    return entry
