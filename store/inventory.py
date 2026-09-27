"""Per-location stock: levels, transfers, counts, multi-line purchase orders with partial receipts and returns."""
import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F, Sum, Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from .models import Product, Supplier, Location, StaffProfile, StockLevel, StockMovement, StockTransfer, StockTransferLine, StockCount, StockCountLine, Purchase, PurchaseLine, PurchaseReceipt, PurchaseReceiptLine, PurchaseReturn, PurchaseReturnLine, AuditLog
from .services import money, integer, journal


def text(d, key, limit, required=False):
    value = d.get(key, '')
    if value is None: value = ''
    if not isinstance(value, str): raise ValueError(f'{key} must be text.')
    value = value.strip()
    if len(value) > limit or (required and not value): raise ValueError(f'{key.replace("_", " ").title()} is required and must be no more than {limit} characters.')
    return value


# --- Locations -----------------------------------------------------------------------------------------------------

def default_location(purpose='pos'):
    """The branch that serves POS sales or online fulfilment; created on first use so single-shop installs need no setup."""
    flag = 'pos_default' if purpose == 'pos' else 'online_default'
    location = Location.objects.filter(active=True, **{flag: True}).first() or Location.objects.filter(active=True).order_by('pk').first()
    if location is None:
        location = Location.objects.create(name='Main store', code='MAIN', pos_default=True, online_default=True)
    return location


def location_for(pk):
    location = Location.objects.filter(pk=integer(pk), active=True).first()
    if location is None: raise ValueError('Choose an active location.')
    return location


def pos_location_for(user, requested=None):
    """A user pinned to a branch always sells from it; otherwise the requested (or default) branch."""
    profile = StaffProfile.objects.filter(user=user).select_related('location').first() if user is not None else None
    if profile and profile.location_id and profile.location.active: return profile.location
    return location_for(requested) if requested else default_location('pos')


# --- Stock primitives ------------------------------------------------------------------------------------------------

def level_for(product, location):
    """Lock and return the level row, materialising it (and the legacy totals) on first touch."""
    level = StockLevel.objects.select_for_update().filter(product=product, location=location).first()
    if level: return level
    if not StockLevel.objects.filter(product=product).exists():
        fresh = Product.objects.only('stock', 'reserved').get(pk=product.pk)
        home = default_location('pos')
        StockLevel.objects.create(product=product, location=home, quantity=fresh.stock, reserved=fresh.reserved)
        if home.pk == location.pk: return StockLevel.objects.select_for_update().get(product=product, location=location)
    return StockLevel.objects.create(product=product, location=location)


def _apply(product, location, quantity_delta, reserved_delta, shown, kind, reference, staff):
    level = level_for(product, location)
    quantity, reserved = level.quantity + quantity_delta, level.reserved + reserved_delta
    if quantity < 0 or reserved < 0 or quantity < reserved:
        raise ValueError(f'Not enough available stock for {product.name} at {location.name}.')
    StockLevel.objects.filter(pk=level.pk).update(quantity=F('quantity') + quantity_delta, reserved=F('reserved') + reserved_delta)
    Product.objects.filter(pk=product.pk).update(stock=F('stock') + quantity_delta, reserved=F('reserved') + reserved_delta)
    product.stock += quantity_delta
    product.reserved += reserved_delta
    StockMovement.objects.create(product=product, location=location, quantity=shown, kind=kind, reference=reference, balance=quantity - reserved, staff=staff)


def take(product, location, qty, kind, reference, staff=None): _apply(product, location, -qty, 0, -qty, kind, reference, staff)
def add(product, location, qty, kind, reference, staff=None): _apply(product, location, qty, 0, qty, kind, reference, staff)
def reserve(product, location, qty, reference, staff=None): _apply(product, location, 0, qty, -qty, 'Reservation', reference, staff)
def release(product, location, qty, kind, reference, staff=None): _apply(product, location, 0, -qty, qty, kind, reference, staff)
def ship(product, location, qty, reference, staff=None): _apply(product, location, -qty, -qty, 0, 'Dispatched', reference, staff)


def note_movement(product, location, kind, reference, staff=None):
    level = StockLevel.objects.filter(product=product, location=location).first()
    StockMovement.objects.create(product=product, location=location, quantity=0, kind=kind, reference=reference, balance=level.available if level else product.available, staff=staff)


def levels_by_product(location):
    return {l.product_id: l for l in StockLevel.objects.filter(location=location)}


def available_at(product, location, levels=None):
    """Availability at one location; products never touched by the location system fall back to their totals."""
    level = (levels or {}).get(product.pk) if levels is not None else StockLevel.objects.filter(product=product, location=location).first()
    if level: return level.available
    return product.available if not StockLevel.objects.filter(product=product).exists() else 0


# --- Purchase orders ---------------------------------------------------------------------------------------------------

def parse_lines(d, key='lines', allow_cost=True):
    lines = d.get(key)
    if lines is None and d.get('product'):
        lines = [{'product': d['product'], 'quantity': d.get('quantity'), 'cost': d.get('cost')}]
    if not isinstance(lines, list) or not lines or len(lines) > 100: raise ValueError('Add between one and 100 lines.')
    parsed = {}
    for item in lines:
        if not isinstance(item, dict): raise ValueError('Each line needs a product and quantity.')
        pk = integer(item.get('product'))
        qty = integer(item.get('quantity'))
        entry = parsed.setdefault(pk, {'quantity': 0, 'cost': None})
        entry['quantity'] += qty
        if allow_cost:
            cost = money(item.get('cost'))
            if entry['cost'] is not None and entry['cost'] != cost: raise ValueError('Use one cost per product in a purchase order.')
            entry['cost'] = cost
    return parsed


@transaction.atomic
def create_purchase(d, user):
    supplier = Supplier.objects.filter(pk=integer(d.get('supplier'))).first()
    if supplier is None: raise ValueError('Choose a supplier.')
    location = location_for(d['location']) if d.get('location') else default_location('pos')
    lines = parse_lines(d)
    products = {p.pk: p for p in Product.objects.filter(pk__in=lines, active=True)}
    if len(products) != len(lines): raise ValueError('A product is no longer available.')
    expected = None
    if d.get('expected_at'):
        expected = parse_date(str(d['expected_at']))
        if expected is None: raise ValueError('Enter a valid expected date.')
    purchase = Purchase.objects.create(supplier=supplier, location=location, expected_at=expected, note=text(d, 'note', 300), created_by=user)
    for pk, entry in lines.items():
        PurchaseLine.objects.create(purchase=purchase, product=products[pk], quantity=entry['quantity'], cost=entry['cost'])
    AuditLog.objects.create(staff=user, action=f'Purchase order created: {purchase.number}', detail=json.dumps({'supplier': supplier.name, 'location': location.name, 'lines': {str(k): v['quantity'] for k, v in lines.items()}, 'total': str(purchase.total)}))
    return purchase


def refresh_purchase_status(purchase):
    lines = list(purchase.lines.all())
    if all(l.received_quantity >= l.quantity for l in lines): purchase.status = 'Received'
    elif any(l.received_quantity for l in lines): purchase.status = 'Partially received'
    else: purchase.status = 'Ordered'
    purchase.save(update_fields=['status'])


@transaction.atomic
def receive_purchase(d, user):
    purchase = Purchase.objects.select_for_update().select_related('location').get(pk=integer(d.get('id')))
    if purchase.status in ['Received', 'Cancelled']: raise ValueError('This purchase has already been received.' if purchase.status == 'Received' else 'This purchase was cancelled.')
    location = purchase.location or default_location('pos')
    lines = {l.pk: l for l in purchase.lines.select_related('product').select_for_update()}
    requested = {}
    if d.get('lines') is None:
        requested = {pk: l.outstanding for pk, l in lines.items() if l.outstanding > 0}
    else:
        if not isinstance(d['lines'], list): raise ValueError('Select the quantities received.')
        for item in d['lines']:
            pk = integer(item.get('line'))
            qty = integer(item.get('quantity', 0), 0)
            if pk not in lines: raise ValueError('That line does not belong to this purchase.')
            if qty == 0: continue
            if qty > lines[pk].outstanding: raise ValueError(f'Only {lines[pk].outstanding} × {lines[pk].product.name} are still expected.')
            requested[pk] = requested.get(pk, 0) + qty
    if not requested: raise ValueError('Enter at least one received quantity.')
    receipt = PurchaseReceipt.objects.create(purchase=purchase, reference=text(d, 'reference', 150), staff=user)
    value = Decimal('0')
    for pk, qty in requested.items():
        line = lines[pk]
        p = Product.objects.select_for_update().get(pk=line.product_id)
        p.cost = ((p.cost * p.stock + line.cost * qty) / (p.stock + qty)).quantize(Decimal('.01')) if p.stock + qty else line.cost
        p.save(update_fields=['cost'])
        add(p, location, qty, 'Purchase received', receipt.number, user)
        PurchaseLine.objects.filter(pk=pk).update(received_quantity=F('received_quantity') + qty)
        PurchaseReceiptLine.objects.create(receipt=receipt, line=line, quantity=qty)
        value += line.cost * qty
    journal(f'{receipt.number} · {purchase.number}', 'Inventory', 'Accounts payable', value)
    refresh_purchase_status(purchase)
    AuditLog.objects.create(staff=user, action=f'Goods received: {receipt.number}', detail=json.dumps({'purchase': purchase.number, 'lines': {str(k): v for k, v in requested.items()}, 'value': str(value)}))
    return receipt


@transaction.atomic
def return_purchase(d, user):
    purchase = Purchase.objects.select_for_update().select_related('location').get(pk=integer(d.get('id')))
    location = purchase.location or default_location('pos')
    lines = {l.pk: l for l in purchase.lines.select_related('product').select_for_update()}
    requested = {}
    for item in d.get('lines') or []:
        pk = integer(item.get('line'))
        qty = integer(item.get('quantity', 0), 0)
        if pk not in lines: raise ValueError('That line does not belong to this purchase.')
        if qty == 0: continue
        if qty > lines[pk].returnable: raise ValueError(f'Only {lines[pk].returnable} × {lines[pk].product.name} can be returned to the supplier.')
        requested[pk] = requested.get(pk, 0) + qty
    if not requested: raise ValueError('Enter at least one quantity to return.')
    reason = text(d, 'reason', 200, True)
    purchase_return = PurchaseReturn.objects.create(purchase=purchase, reason=reason, total=0, staff=user)
    value = Decimal('0')
    for pk, qty in requested.items():
        line = lines[pk]
        take(line.product, location, qty, 'Purchase return', purchase_return.number, user)
        PurchaseLine.objects.filter(pk=pk).update(returned_quantity=F('returned_quantity') + qty)
        PurchaseReturnLine.objects.create(purchase_return=purchase_return, line=line, quantity=qty)
        value += line.cost * qty
    purchase_return.total = value
    purchase_return.save(update_fields=['total'])
    journal(f'{purchase_return.number} · {purchase.number}', 'Accounts payable', 'Inventory', value)
    AuditLog.objects.create(staff=user, action=f'Returned to supplier: {purchase_return.number}', detail=json.dumps({'purchase': purchase.number, 'reason': reason, 'lines': {str(k): v for k, v in requested.items()}, 'value': str(value)}))
    return purchase_return


@transaction.atomic
def cancel_purchase(d, user):
    purchase = Purchase.objects.select_for_update().get(pk=integer(d.get('id')))
    if purchase.lines.filter(received_quantity__gt=0).exists(): raise ValueError('Goods have already been received against this order; return them instead.')
    if purchase.status == 'Cancelled': raise ValueError('Already cancelled.')
    purchase.status = 'Cancelled'
    purchase.save(update_fields=['status'])
    AuditLog.objects.create(staff=user, action=f'Purchase order cancelled: {purchase.number}', detail=text(d, 'reason', 200))
    return purchase


# --- Transfers -----------------------------------------------------------------------------------------------------------

@transaction.atomic
def create_transfer(d, user):
    source, destination = location_for(d.get('source')), location_for(d.get('destination'))
    if source.pk == destination.pk: raise ValueError('Choose two different locations.')
    lines = parse_lines(d, allow_cost=False)
    products = {p.pk: p for p in Product.objects.filter(pk__in=lines, active=True)}
    if len(products) != len(lines): raise ValueError('A product is no longer available.')
    transfer = StockTransfer.objects.create(source=source, destination=destination, note=text(d, 'note', 300), created_by=user)
    for pk, entry in lines.items(): StockTransferLine.objects.create(transfer=transfer, product=products[pk], quantity=entry['quantity'])
    if d.get('dispatch'): dispatch_transfer({'id': transfer.pk}, user)
    AuditLog.objects.create(staff=user, action=f'Transfer created: {transfer.number}', detail=f'{source.name} → {destination.name}')
    return transfer


@transaction.atomic
def dispatch_transfer(d, user):
    transfer = StockTransfer.objects.select_for_update().select_related('source', 'destination').get(pk=integer(d.get('id')))
    if transfer.status != 'Draft': raise ValueError('Only draft transfers can be dispatched.')
    for line in transfer.lines.select_related('product'):
        take(line.product, transfer.source, line.quantity, 'Transfer out', transfer.number, user)
    transfer.status, transfer.dispatched_at = 'Dispatched', timezone.now()
    transfer.save(update_fields=['status', 'dispatched_at'])
    AuditLog.objects.create(staff=user, action=f'Transfer dispatched: {transfer.number}', detail=f'{transfer.source.name} → {transfer.destination.name}')
    return transfer


@transaction.atomic
def receive_transfer(d, user):
    transfer = StockTransfer.objects.select_for_update().select_related('source', 'destination').get(pk=integer(d.get('id')))
    if transfer.status not in ['Dispatched', 'Partially received']: raise ValueError('This transfer is not in transit.')
    lines = {l.pk: l for l in transfer.lines.select_related('product').select_for_update()}
    received, written_off = {}, {}
    for item in d.get('lines') or [{'line': pk, 'quantity': l.quantity - l.received_quantity - l.written_off} for pk, l in lines.items()]:
        pk = integer(item.get('line'))
        if pk not in lines: raise ValueError('That line does not belong to this transfer.')
        line = lines[pk]
        in_transit = line.quantity - line.received_quantity - line.written_off
        qty = integer(item.get('quantity', 0), 0)
        lost = integer(item.get('written_off', 0), 0)
        if qty + lost > in_transit: raise ValueError(f'Only {in_transit} × {line.product.name} are in transit.')
        if qty: received[pk] = qty
        if lost: written_off[pk] = lost
    if not received and not written_off: raise ValueError('Enter at least one received quantity.')
    loss = Decimal('0')
    for pk, qty in received.items():
        add(lines[pk].product, transfer.destination, qty, 'Transfer in', transfer.number, user)
        StockTransferLine.objects.filter(pk=pk).update(received_quantity=F('received_quantity') + qty)
    for pk, qty in written_off.items():
        StockTransferLine.objects.filter(pk=pk).update(written_off=F('written_off') + qty)
        note_movement(lines[pk].product, transfer.destination, 'Lost in transit', transfer.number, user)
        loss += lines[pk].product.cost * qty
    journal(transfer.number, 'Inventory loss', 'Inventory', loss)
    remaining = sum(l.quantity - l.received_quantity - l.written_off for l in transfer.lines.all())
    transfer.status = 'Received' if remaining == 0 else 'Partially received'
    if remaining == 0: transfer.received_at = timezone.now()
    transfer.save(update_fields=['status', 'received_at'])
    AuditLog.objects.create(staff=user, action=f'Transfer received: {transfer.number}', detail=json.dumps({'received': {str(k): v for k, v in received.items()}, 'written_off': {str(k): v for k, v in written_off.items()}}))
    return transfer


@transaction.atomic
def cancel_transfer(d, user):
    transfer = StockTransfer.objects.select_for_update().select_related('source').get(pk=integer(d.get('id')))
    if transfer.status == 'Draft':
        pass
    elif transfer.status == 'Dispatched':
        for line in transfer.lines.select_related('product'):
            add(line.product, transfer.source, line.quantity, 'Transfer cancelled', transfer.number, user)
    else:
        raise ValueError('Partially received transfers cannot be cancelled; receive or write off the remainder.')
    transfer.status = 'Cancelled'
    transfer.save(update_fields=['status'])
    AuditLog.objects.create(staff=user, action=f'Transfer cancelled: {transfer.number}', detail=text(d, 'reason', 200))
    return transfer


# --- Stock counts ---------------------------------------------------------------------------------------------------------

@transaction.atomic
def start_count(d, user):
    location = location_for(d.get('location'))
    category = text(d, 'category', 80)
    if StockCount.objects.filter(location=location, status='Open').exists(): raise ValueError(f'A count is already open at {location.name}. Complete or cancel it first.')
    products = Product.objects.filter(active=True)
    if category: products = products.filter(category=category)
    levels = levels_by_product(location)
    count = StockCount.objects.create(location=location, category=category, note=text(d, 'note', 300), created_by=user)
    for p in products.order_by('name'):
        StockCountLine.objects.create(count=count, product=p, expected=levels[p.pk].quantity if p.pk in levels else (p.stock if not StockLevel.objects.filter(product=p).exists() and location.pos_default else 0))
    AuditLog.objects.create(staff=user, action=f'Stock count started: {count.number}', detail=f'{location.name} · {category or "all categories"}')
    return count


@transaction.atomic
def record_count(d, user):
    count = StockCount.objects.select_for_update().get(pk=integer(d.get('id')))
    if count.status != 'Open': raise ValueError('This count is closed.')
    entries = d.get('lines')
    if not isinstance(entries, list): raise ValueError('Send the counted quantities.')
    lines = {l.pk: l for l in count.lines.all()}
    for item in entries:
        pk = integer(item.get('line'))
        if pk not in lines: raise ValueError('That line does not belong to this count.')
        value = item.get('counted')
        StockCountLine.objects.filter(pk=pk).update(counted=None if value in [None, ''] else integer(value, 0))
    return count


@transaction.atomic
def complete_count(d, user):
    count = StockCount.objects.select_for_update().select_related('location').get(pk=integer(d.get('id')))
    if count.status != 'Open': raise ValueError('This count is closed.')
    if d.get('lines'): record_count(d, user)
    gains, losses, adjusted = Decimal('0'), Decimal('0'), 0
    for line in count.lines.select_related('product').filter(counted__isnull=False):
        level = StockLevel.objects.filter(product=line.product, location=count.location).first()
        current = level.quantity if level else line.expected
        delta = line.counted - current
        if delta == 0: continue
        if delta > 0:
            add(line.product, count.location, delta, 'Stock count', count.number, user)
            gains += line.product.cost * delta
        else:
            take(line.product, count.location, -delta, 'Stock count', count.number, user)
            losses += line.product.cost * -delta
        adjusted += 1
    journal(count.number, 'Inventory', 'Inventory gain', gains)
    journal(count.number, 'Inventory loss', 'Inventory', losses)
    count.status, count.completed_by, count.completed_at = 'Completed', user, timezone.now()
    count.save(update_fields=['status', 'completed_by', 'completed_at'])
    AuditLog.objects.create(staff=user, action=f'Stock count completed: {count.number}', detail=json.dumps({'location': count.location.name, 'adjusted_products': adjusted, 'gain': str(gains), 'loss': str(losses)}))
    return count


@transaction.atomic
def cancel_count(d, user):
    count = StockCount.objects.select_for_update().get(pk=integer(d.get('id')))
    if count.status != 'Open': raise ValueError('This count is closed.')
    count.status = 'Cancelled'
    count.save(update_fields=['status'])
    AuditLog.objects.create(staff=user, action=f'Stock count cancelled: {count.number}', detail='')
    return count


# --- Location administration -------------------------------------------------------------------------------------------------

@transaction.atomic
def save_location(d, user):
    location = Location.objects.select_for_update().get(pk=integer(d['id'])) if d.get('id') else Location()
    location.name = text(d, 'name', 100, True)
    location.code = text(d, 'code', 12, True).upper()
    location.kind = d.get('kind', location.kind or 'store')
    if location.kind not in dict(Location.KINDS): raise ValueError('Choose store or warehouse.')
    location.address = text(d, 'address', 300)
    location.phone = text(d, 'phone', 40)
    active = d.get('active', True)
    if not isinstance(active, bool): raise ValueError('Active must be true or false.')
    if not active and location.pk and (location.pos_default or location.online_default): raise ValueError('Make another location the default before deactivating this one.')
    if not active and location.pk and StockLevel.objects.filter(location=location, quantity__gt=0).exists(): raise ValueError('Transfer the remaining stock out before deactivating this location.')
    location.active = active
    location.save()
    for flag in ['pos_default', 'online_default']:
        if d.get(flag):
            Location.objects.exclude(pk=location.pk).update(**{flag: False})
            Location.objects.filter(pk=location.pk).update(**{flag: True})
    AuditLog.objects.create(staff=user, action=f'Location saved: {location.code}', detail=location.name)
    return Location.objects.get(pk=location.pk)


@transaction.atomic
def assign_staff_location(d, user):
    User = get_user_model()
    staff = User.objects.filter(pk=integer(d.get('user')), is_staff=True).first()
    if staff is None: raise ValueError('Choose a staff account.')
    location = location_for(d['location']) if d.get('location') else None
    profile, _ = StaffProfile.objects.get_or_create(user=staff)
    profile.location = location
    profile.save()
    AuditLog.objects.create(staff=user, action=f'Staff location set: {staff.get_username()}', detail=location.name if location else 'Any location')
    return profile


# --- Serialisers and views -------------------------------------------------------------------------------------------------------

def location_data(l): return {'id': l.pk, 'name': l.name, 'code': l.code, 'kind': l.kind, 'address': l.address, 'phone': l.phone, 'active': l.active, 'pos_default': l.pos_default, 'online_default': l.online_default}


def purchase_data(p, finance=True):
    lines = list(p.lines.select_related('product'))
    return {'id': p.pk, 'number': p.number, 'supplier': p.supplier.name, 'supplier_id': p.supplier_id, 'location': p.location.name if p.location else '', 'location_id': p.location_id, 'status': p.status, 'expected_at': p.expected_at.isoformat() if p.expected_at else None, 'note': p.note, 'date': p.created_at.isoformat(),
            'product': lines[0].product.name if len(lines) == 1 else f'{len(lines)} products', 'quantity': sum(l.quantity for l in lines), 'received_quantity': sum(l.received_quantity for l in lines), 'received': p.status == 'Received',
            'total': float(p.total) if finance else None, 'received_value': float(p.received_value) if finance else None, 'payable': float(p.payable) if finance else None, 'paid_total': float(p.paid_total) if finance else None, 'due': float(p.payable - p.paid_total) if finance else 0, 'cost': float(lines[0].cost) if finance and len(lines) == 1 else None,
            'lines': [{'id': l.pk, 'product': l.product_id, 'name': l.product.name, 'sku': l.product.sku, 'quantity': l.quantity, 'received_quantity': l.received_quantity, 'returned_quantity': l.returned_quantity, 'outstanding': l.outstanding, 'returnable': l.returnable, 'cost': float(l.cost) if finance else None} for l in lines],
            'receipts': [{'id': r.pk, 'number': r.number, 'reference': r.reference, 'date': r.created_at.isoformat(), 'staff': r.staff.get_username() if r.staff else ''} for r in p.receipts.all()],
            'returns': [{'id': r.pk, 'number': r.number, 'reason': r.reason, 'total': float(r.total) if finance else None, 'date': r.created_at.isoformat()} for r in p.returns.all()]}


def transfer_data(t):
    return {'id': t.pk, 'number': t.number, 'source': t.source.name, 'source_id': t.source_id, 'destination': t.destination.name, 'destination_id': t.destination_id, 'status': t.status, 'note': t.note, 'date': t.created_at.isoformat(), 'dispatched_at': t.dispatched_at.isoformat() if t.dispatched_at else None, 'received_at': t.received_at.isoformat() if t.received_at else None, 'created_by': t.created_by.get_username() if t.created_by else '',
            'lines': [{'id': l.pk, 'product': l.product_id, 'name': l.product.name, 'sku': l.product.sku, 'quantity': l.quantity, 'received_quantity': l.received_quantity, 'written_off': l.written_off, 'in_transit': l.quantity - l.received_quantity - l.written_off} for l in t.lines.select_related('product')]}


def count_data(c, with_lines=False):
    data = {'id': c.pk, 'number': c.number, 'location': c.location.name, 'location_id': c.location_id, 'status': c.status, 'category': c.category, 'note': c.note, 'date': c.created_at.isoformat(), 'completed_at': c.completed_at.isoformat() if c.completed_at else None, 'created_by': c.created_by.get_username() if c.created_by else '', 'lines_total': c.lines.count(), 'lines_counted': c.lines.filter(counted__isnull=False).count()}
    if with_lines: data['lines'] = [{'id': l.pk, 'product': l.product_id, 'name': l.product.name, 'sku': l.product.sku, 'barcode': l.product.barcode, 'expected': l.expected, 'counted': l.counted} for l in c.lines.select_related('product').order_by('product__name')]
    return data


def permitted(user, roles):
    return user.is_authenticated and user.is_staff and (user.is_superuser or user.groups.filter(name__in=roles).exists())


def inventory_access(user): return permitted(user, ['Owner', 'Manager', 'Inventory Manager'])
def manager_access(user): return permitted(user, ['Owner', 'Manager'])


@require_GET
@never_cache
def overview(request):
    if not (request.user.is_authenticated and request.user.is_staff): return JsonResponse({'error': 'Staff login required.'}, status=403)
    count_id = request.GET.get('count')
    User = get_user_model()
    staff = [{'id': u.pk, 'username': u.get_username(), 'name': u.get_full_name() or u.get_username(), 'location': getattr(getattr(u, 'store_profile', None), 'location_id', None)} for u in User.objects.filter(is_staff=True, is_active=True).select_related('store_profile').order_by('username')] if manager_access(request.user) else []
    return JsonResponse({
        'locations': [location_data(l) for l in Location.objects.all()],
        'transfers': [transfer_data(t) for t in StockTransfer.objects.select_related('source', 'destination', 'created_by').order_by('-pk')[:100]],
        'counts': [count_data(c) for c in StockCount.objects.select_related('location', 'created_by').order_by('-pk')[:50]],
        'count': count_data(StockCount.objects.select_related('location').get(pk=count_id), with_lines=True) if count_id and StockCount.objects.filter(pk=count_id).exists() else None,
        'staff': staff,
    })


ACTIONS = {
    'purchase': (create_purchase, inventory_access), 'receive': (receive_purchase, inventory_access), 'purchase_return': (return_purchase, inventory_access), 'purchase_cancel': (cancel_purchase, inventory_access),
    'transfer': (create_transfer, inventory_access), 'dispatch': (dispatch_transfer, inventory_access), 'transfer_receive': (receive_transfer, inventory_access), 'transfer_cancel': (cancel_transfer, inventory_access),
    'count_start': (start_count, inventory_access), 'count_record': (record_count, inventory_access), 'count_complete': (complete_count, inventory_access), 'count_cancel': (cancel_count, inventory_access),
    'location': (save_location, manager_access), 'staff_location': (assign_staff_location, manager_access),
}


@require_POST
def action(request):
    try:
        d = json.loads(request.body)
        if not isinstance(d, dict): raise ValueError('Expected an object.')
        kind = d.get('action')
        if kind not in ACTIONS: raise ValueError('Unknown inventory action.')
        handler, gate = ACTIONS[kind]
        if not gate(request.user): return JsonResponse({'error': 'Your role does not have permission for this action.'}, status=403)
        result = handler(d, request.user)
        payload = {'ok': True}
        if isinstance(result, Purchase): payload['purchase'] = purchase_data(result)
        elif isinstance(result, PurchaseReceipt): payload['receipt'] = result.number
        elif isinstance(result, PurchaseReturn): payload['return'] = result.number
        elif isinstance(result, StockTransfer): payload['transfer'] = transfer_data(result)
        elif isinstance(result, StockCount): payload['count'] = count_data(result, with_lines=True)
        elif isinstance(result, Location): payload['location'] = location_data(result)
        return JsonResponse(payload)
    except (ValueError, TypeError, KeyError, Purchase.DoesNotExist, StockTransfer.DoesNotExist, StockCount.DoesNotExist, Location.DoesNotExist) as exc:
        return JsonResponse({'error': 'A location code must be unique.' if isinstance(exc, IntegrityError) else str(exc)}, status=400)
    except IntegrityError:
        return JsonResponse({'error': 'A location code must be unique.'}, status=400)
    except OperationalError:
        return JsonResponse({'error': 'The database is busy. Please try again.'}, status=409)
