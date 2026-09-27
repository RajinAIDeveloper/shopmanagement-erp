import logging
from decimal import Decimal, InvalidOperation
from django.db import transaction, OperationalError
from django.db.models import F
from django.utils import timezone
from .models import Product, Order, OrderLine, StockMovement, JournalEntry, AuditLog, StoreProfile

logger = logging.getLogger('store')


def money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal('.01'))
        if not result.is_finite() or result < 0 or result > Decimal('999999999'): raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Enter a valid, non-negative amount.')


def integer(value, minimum=1):
    try:
        result = int(str(value))
        if result < minimum or abs(result) > 1000000: raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError(f'Enter a whole number of at least {minimum}.')


def movement(product, quantity, kind, reference, staff=None):
    product.refresh_from_db()
    StockMovement.objects.create(product=product, quantity=quantity, kind=kind, reference=reference, balance=product.available, staff=staff)


def journal(reference, debit, credit, amount):
    from .accounting import post_pair
    return post_pair(reference, debit, credit, amount)


def create_order(items, channel, payment, customer=None, discount=0, staff=None, **options):
    """Create a POS or online order. The money handling lives in selling.create_order; this keeps the historical import path."""
    from .selling import create_order as create_sale
    return create_sale(items, channel, payment, customer, discount, staff, **options)


@transaction.atomic
def transition_order(pk, status, staff):
    order = Order.objects.select_for_update().get(pk=pk)
    if order.status=='Pending' and order.reservation_expires_at and order.reservation_expires_at<=timezone.now():
        raise ValueError('This reservation has expired. Refresh the orders list.')
    allowed = {'Pending': ['Confirmed', 'Cancelled'], 'Confirmed': ['Packed', 'Cancelled'], 'Packed': ['Shipped', 'Cancelled'], 'Shipped': ['Delivered'], 'Delivered': ['Returned'], 'Completed': ['Returned'], 'Partially returned': ['Returned']}
    if status not in allowed.get(order.status, []): raise ValueError('This order status transition is not allowed.')
    if status == 'Returned':
        from .selling import process_return
        process_return(order.pk, None, 'Full return', True, 'later', 'return', staff)
        order.refresh_from_db()
        return order
    from .inventory import release, ship, default_location
    place = order.location or default_location('online')
    cogs = Decimal('0')
    for line in order.lines.select_related('product'):
        p = line.product
        cogs += line.cost * line.quantity
        if status == 'Cancelled':
            release(p, place, line.quantity, 'Reservation released', order.number, staff)
        elif status == 'Shipped':
            ship(p, place, line.quantity, order.number, staff)
    if status == 'Shipped':
        journal(order.number, 'Accounts receivable', 'Sales revenue', order.total)
        journal(order.number, 'Sales revenue', 'VAT payable', order.tax_total)
        journal(order.number, 'Sales revenue', 'Shipping revenue', order.shipping_total-order.shipping_tax)
        journal(order.number, 'Cost of goods sold', 'Inventory', cogs)
    AuditLog.objects.create(staff=staff, action=f'{order.number}: {order.status} → {status}', detail='')
    order.status = status
    order.reservation_expires_at = None
    order.save(update_fields=['status','reservation_expires_at'])
    from .customers import loyalty_sync, release_promotions
    if status == 'Cancelled': release_promotions(order)
    if status == 'Delivered': loyalty_sync(order)
    return order


@transaction.atomic
def expire_reservation(pk, at):
    order=Order.objects.select_for_update().filter(pk=pk,channel='Online',status='Pending',reservation_expires_at__lte=at).first()
    if not order: return False
    from .customers import release_promotions
    release_promotions(order)
    if not Order.objects.filter(pk=pk,status='Pending',reservation_expires_at__lte=at).update(status='Expired',reservation_expires_at=None): return False
    from .inventory import release, default_location
    place=order.location or default_location('online')
    for line in order.lines.select_related('product').order_by('product_id'):
        release(line.product,place,line.quantity,'Reservation expired',order.number)
    AuditLog.objects.create(action=f'{order.number}: Pending → Expired',detail='Unconfirmed reservation expired; all reserved stock released automatically.')
    return True


def expire_due_reservations(limit=500):
    at=timezone.now()
    ids=list(Order.objects.filter(channel='Online',status='Pending',reservation_expires_at__lte=at).order_by('reservation_expires_at').values_list('pk',flat=True)[:limit])
    return sum(expire_reservation(pk,at) for pk in ids)


def sweep_reservations():
    """Best-effort release before a write; a busy database must never turn into a failed request."""
    try:
        return expire_due_reservations()
    except OperationalError:
        logger.warning('Reservation sweep skipped: database busy')
        return 0


def display_status(order, at=None):
    """The status a reader should see: an overdue unconfirmed reservation is already 'Expired' even before the sweep runs."""
    if order.status=='Pending' and order.reservation_expires_at and order.reservation_expires_at<=(at or timezone.now()):
        return 'Expired'
    return order.status
