from decimal import Decimal
from django.db.models import Q
from django.utils import timezone
from .models import Offer


def live_records(queryset, at=None):
    at = at or timezone.now()
    return queryset.filter(active=True).filter(Q(starts_at__isnull=True) | Q(starts_at__lte=at)).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=at))


def offer_price(product, offers):
    """Online promotions never stack; choose the best currently eligible offer."""
    best, selected = product.price, None
    for offer in offers:
        if offer.product_id and offer.product_id != product.pk:
            continue
        if offer.category and offer.category != product.category:
            continue
        discount = product.price * offer.value / 100 if offer.discount_type == 'percent' else offer.value
        price = max(Decimal('0'), product.price - discount).quantize(Decimal('.01'))
        if price < best:
            best, selected = price, offer
    return best, selected


def active_offers():
    return list(live_records(Offer.objects.all()).order_by('pk'))
