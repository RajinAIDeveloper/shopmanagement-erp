import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone


class Location(models.Model):
    """A branch shop or warehouse holding its own stock."""
    KINDS = [('store', 'Store'), ('warehouse', 'Warehouse')]
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=12, unique=True)
    kind = models.CharField(max_length=10, choices=KINDS, default='store')
    address = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    active = models.BooleanField(default=True)
    pos_default = models.BooleanField(default=False)
    online_default = models.BooleanField(default=False)
    def __str__(self): return self.name
    class Meta:
        ordering = ['pk']


class StaffProfile(models.Model):
    """Per-user workspace settings; a fixed location pins that user's POS to one branch."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='store_profile')
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT)


class Supplier(models.Model):
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    def __str__(self): return self.name


class Product(models.Model):
    name = models.CharField(max_length=180)
    sku = models.CharField(max_length=60, unique=True)
    barcode = models.CharField(max_length=80, unique=True)
    category = models.CharField(max_length=80)
    brand = models.CharField(max_length=80, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=10)
    image = models.CharField(max_length=400, blank=True)
    photo = models.CharField(max_length=400, blank=True)
    vat_exempt = models.BooleanField(default=False)
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    min_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT)
    @property
    def available(self): return self.stock - self.reserved
    def __str__(self): return self.name
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(stock__gte=models.F('reserved')), name='reserved_within_stock'), models.CheckConstraint(condition=models.Q(price__gte=0, cost__gte=0), name='positive_prices')]


class Customer(models.Model):
    TIERS = [('retail', 'Retail'), ('wholesale', 'Wholesale')]
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    pricing_tier = models.CharField(max_length=10, choices=TIERS, default='retail')
    credit_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='customer_record')
    birthday = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    loyalty_points = models.IntegerField(default=0)
    def __str__(self): return self.name


class Order(models.Model):
    CHANNELS = [('POS', 'POS'), ('Online', 'Online')]
    tracking_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT)
    channel = models.CharField(max_length=10, choices=CHANNELS)
    status = models.CharField(max_length=30, default='Completed')
    payment = models.CharField(max_length=30, default='Cash')
    total = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refunded_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    returned_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_zone = models.CharField(max_length=100, blank=True)
    coupon_code = models.CharField(max_length=40, blank=True)
    loyalty_redeemed = models.PositiveIntegerField(default=0)
    reservation_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    shift = models.ForeignKey('Shift', null=True, blank=True, on_delete=models.PROTECT, related_name='orders')
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT, related_name='orders')
    request_id = models.UUIDField(null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    @property
    def number(self): return f'AT-{self.pk:05d}'
    @property
    def net_total(self): return self.total - self.returned_total
    @property
    def due(self): return max(Decimal('0'), self.net_total - self.paid_total)
    @property
    def refund_due(self): return max(Decimal('0'), self.paid_total - self.net_total) - self.refunded_total
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(paid_total__gte=0, refunded_total__gte=0, returned_total__gte=0, paid_total__lte=models.F('total'), refunded_total__lte=models.F('paid_total'), returned_total__lte=models.F('total')), name='valid_order_payments')]


class OrderLine(models.Model):
    order = models.ForeignKey(Order, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    name = models.CharField(max_length=180, blank=True)
    sku = models.CharField(max_length=60, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    returned_quantity = models.PositiveIntegerField(default=0)
    @property
    def subtotal(self): return self.price * self.quantity
    @property
    def returnable(self): return self.quantity - self.returned_quantity


class StockMovement(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT)
    quantity = models.IntegerField()
    kind = models.CharField(max_length=40)
    reference = models.CharField(max_length=150)
    balance = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)


class Purchase(models.Model):
    """A purchase order: one supplier, one receiving location, many lines, received in one or more deliveries."""
    STATUSES = [('Ordered', 'Ordered'), ('Partially received', 'Partially received'), ('Received', 'Received'), ('Cancelled', 'Cancelled')]
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT, related_name='purchases')
    status = models.CharField(max_length=20, choices=STATUSES, default='Ordered')
    expected_at = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    paid_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def number(self): return f'PO-{self.pk:04d}'
    @property
    def received(self): return self.status == 'Received'
    @property
    def total(self): return sum((l.quantity * l.cost for l in self.lines.all()), Decimal('0'))
    @property
    def received_value(self): return sum((l.received_quantity * l.cost for l in self.lines.all()), Decimal('0'))
    @property
    def returned_value(self): return sum((l.returned_quantity * l.cost for l in self.lines.all()), Decimal('0'))
    @property
    def payable(self): return self.received_value - self.returned_value
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(paid_total__gte=0), name='valid_purchase_payments')]


class PurchaseLine(models.Model):
    purchase = models.ForeignKey(Purchase, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    received_quantity = models.PositiveIntegerField(default=0)
    returned_quantity = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    @property
    def outstanding(self): return self.quantity - self.received_quantity
    @property
    def returnable(self): return self.received_quantity - self.returned_quantity
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(received_quantity__lte=models.F('quantity'), returned_quantity__lte=models.F('received_quantity')), name='purchase_line_quantities')]


class PurchaseReceipt(models.Model):
    """One delivery against a purchase order."""
    purchase = models.ForeignKey(Purchase, related_name='receipts', on_delete=models.CASCADE)
    reference = models.CharField(max_length=150, blank=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def number(self): return f'GRN-{self.pk:05d}'


class PurchaseReceiptLine(models.Model):
    receipt = models.ForeignKey(PurchaseReceipt, related_name='lines', on_delete=models.CASCADE)
    line = models.ForeignKey(PurchaseLine, related_name='receipt_lines', on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()


class PurchaseReturn(models.Model):
    """Goods sent back to the supplier from a received purchase."""
    purchase = models.ForeignKey(Purchase, related_name='returns', on_delete=models.PROTECT)
    reason = models.CharField(max_length=200)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def number(self): return f'PR-{self.pk:05d}'


class PurchaseReturnLine(models.Model):
    purchase_return = models.ForeignKey(PurchaseReturn, related_name='lines', on_delete=models.CASCADE)
    line = models.ForeignKey(PurchaseLine, related_name='return_lines', on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()


class Expense(models.Model):
    description = models.CharField(max_length=180)
    category = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)


class JournalEntry(models.Model):
    reference = models.CharField(max_length=150)
    debit = models.CharField(max_length=80)
    credit = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    effective_date = models.DateField(default=timezone.localdate, db_index=True)
    voucher = models.ForeignKey('JournalVoucher', null=True, blank=True, on_delete=models.PROTECT, related_name='entries')


class Account(models.Model):
    TYPES = [(x, x.title()) for x in ['asset', 'liability', 'equity', 'income', 'expense']]
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=12, choices=TYPES)
    cash = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    system = models.BooleanField(default=False)
    cash_flow = models.CharField(max_length=12, default='operating', choices=[(x, x.title()) for x in ['operating', 'investing', 'financing']])
    class Meta:
        ordering = ['code']


class FiscalPeriod(models.Model):
    name = models.CharField(max_length=80)
    start = models.DateField()
    end = models.DateField()
    closed = models.BooleanField(default=False)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    closed_at = models.DateTimeField(null=True)
    class Meta:
        ordering = ['-start']
        constraints = [models.CheckConstraint(condition=models.Q(end__gte=models.F('start')), name='fiscal_dates_valid')]


class JournalVoucher(models.Model):
    reference = models.CharField(max_length=150)
    memo = models.CharField(max_length=500)
    date = models.DateField()
    request_id = models.UUIDField(unique=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reverses = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='reversal')
    created_at = models.DateTimeField(auto_now_add=True)


class BankReconciliation(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    start = models.DateField()
    end = models.DateField()
    opening = models.DecimalField(max_digits=14, decimal_places=2)
    closing = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=150)
    status = models.CharField(max_length=12, default='Draft')
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class BankStatementLine(models.Model):
    reconciliation = models.ForeignKey(BankReconciliation, related_name='lines', on_delete=models.PROTECT)
    date = models.DateField()
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['entry', 'account'], name='bank_entry_matched_once')]


class AuditLog(models.Model):
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class StoreProfile(models.Model):
    name = models.CharField(max_length=100, default='Altab Tech')
    tagline = models.CharField(max_length=200, default='Good tech. Better everyday.')
    description = models.CharField(max_length=500, default='Thoughtfully chosen essentials for work, play, and everything in between.')
    address = models.CharField(max_length=300, default='Dhaka, Bangladesh')
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    logo = models.CharField(max_length=400, blank=True)
    primary_color = models.CharField(max_length=7, default='#176b50')
    accent_color = models.CharField(max_length=7, default='#e8eddf')
    theme = models.CharField(max_length=20, default='light')
    announcement_speed = models.PositiveIntegerField(default=35)
    reservation_hours = models.PositiveIntegerField(default=24)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))
    prices_include_vat = models.BooleanField(default=True)
    vat_registration = models.CharField(max_length=40, blank=True)
    receipt_footer = models.CharField(max_length=300, blank=True, default='Thank you for shopping with us.')
    require_shift = models.BooleanField(default=False)
    loyalty_enabled = models.BooleanField(default=False)
    loyalty_earn_step = models.PositiveIntegerField(default=100)

    @classmethod
    def current(cls):
        return cls.objects.get_or_create(pk=1)[0]


class CategoryAttribute(models.Model):
    category = models.CharField(max_length=80)
    name = models.CharField(max_length=80)
    options = models.JSONField(default=list)
    position = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['position', 'name']
        constraints = [models.UniqueConstraint(fields=['category','name'], name='unique_category_attribute')]


class Offer(models.Model):
    title = models.CharField(max_length=120)
    description = models.CharField(max_length=600, blank=True)
    image = models.CharField(max_length=400, blank=True)
    category = models.CharField(max_length=80, blank=True)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.CASCADE)
    discount_type = models.CharField(max_length=10, choices=[('percent','Percentage'),('fixed','Fixed amount')], default='percent')
    value = models.DecimalField(max_digits=12, decimal_places=2)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)


class Announcement(models.Model):
    message = models.CharField(max_length=300)
    link = models.CharField(max_length=400, blank=True)
    position = models.PositiveIntegerField(default=0)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['position', 'pk']


class Review(models.Model):
    product = models.ForeignKey(Product, related_name='reviews', on_delete=models.CASCADE)
    author = models.CharField(max_length=80)
    rating = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000)
    visitor = models.UUIDField()
    status = models.CharField(max_length=10, default='pending', choices=[('pending','Pending'),('approved','Approved'),('hidden','Hidden')])
    created_at = models.DateTimeField(auto_now_add=True)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT)
    verified_purchase = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.CheckConstraint(condition=models.Q(rating__gte=1, rating__lte=5), name='review_rating_1_to_5'), models.UniqueConstraint(fields=['product','visitor'], name='one_review_per_visitor_product')]


class WishlistItem(models.Model):
    customer = models.ForeignKey(Customer, related_name='wishlist_items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['customer', 'product'], name='customer_wishlist_unique')]


class Coupon(models.Model):
    code = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=10, choices=[('fixed', 'Fixed'), ('percent', 'Percent')])
    value = models.DecimalField(max_digits=12, decimal_places=2)
    minimum = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    max_uses = models.PositiveIntegerField(default=100)
    per_customer = models.PositiveIntegerField(default=1)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    members_only = models.BooleanField(default=False)


class CouponUse(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.PROTECT, related_name='uses')
    order = models.OneToOneField(Order, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    identity = models.CharField(max_length=64)
    released = models.BooleanField(default=False)


class LoyaltyEntry(models.Model):
    customer = models.ForeignKey(Customer, related_name='loyalty_entries', on_delete=models.PROTECT)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT)
    points = models.IntegerField()
    reason = models.CharField(max_length=150)
    event = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ShippingZone(models.Model):
    name = models.CharField(max_length=100, unique=True)
    charge = models.DecimalField(max_digits=12, decimal_places=2)
    free_above = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    active = models.BooleanField(default=True)


class Settlement(models.Model):
    KINDS = [('collection','Customer collection'),('supplier_payment','Supplier payment'),('refund','Customer refund'),('reversal','Reversal')]
    kind = models.CharField(max_length=20, choices=KINDS)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT, related_name='settlements')
    purchase = models.ForeignKey(Purchase, null=True, blank=True, on_delete=models.PROTECT, related_name='settlements')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20)
    reference = models.CharField(max_length=150)
    note = models.CharField(max_length=500, blank=True)
    request_id = models.UUIDField(unique=True)
    reverses = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='reversal')
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    shift = models.ForeignKey('Shift', null=True, blank=True, on_delete=models.PROTECT, related_name='settlements')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='positive_settlement'), models.CheckConstraint(condition=(models.Q(order__isnull=False,purchase__isnull=True)|models.Q(order__isnull=True,purchase__isnull=False)),name='one_settlement_target')]


class OrderPayment(models.Model):
    """One tender against a sale: a POS sale may be paid with several, and money can be left owing by account customers."""
    METHODS = ['Cash', 'Card', 'bKash', 'Nagad', 'Bank', 'Store credit', 'Exchange credit']
    order = models.ForeignKey(Order, related_name='payments', on_delete=models.CASCADE)
    method = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='positive_order_payment')]


class SaleReturn(models.Model):
    """Goods coming back against a sale: a partial or full return, or the first half of an exchange."""
    KINDS = [('return', 'Return'), ('exchange', 'Exchange')]
    order = models.ForeignKey(Order, related_name='returns', on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=KINDS, default='return')
    reason = models.CharField(max_length=200)
    restock = models.BooleanField(default=True)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refund_method = models.CharField(max_length=20, default='later')
    exchange_order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT, related_name='exchange_for')
    exchange_used = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def number(self): return f'RT-{self.pk:05d}'


class SaleReturnLine(models.Model):
    sale_return = models.ForeignKey(SaleReturn, related_name='lines', on_delete=models.CASCADE)
    line = models.ForeignKey(OrderLine, related_name='return_lines', on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)


class CreditTransaction(models.Model):
    """Store-credit ledger per customer; the running balance lives on Customer.credit_balance."""
    KINDS = [('refund', 'Refund to credit'), ('redeemed', 'Redeemed at sale'), ('adjustment', 'Manual adjustment'), ('reversal', 'Reversal')]
    customer = models.ForeignKey(Customer, related_name='credit_transactions', on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=KINDS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=150)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Shift(models.Model):
    """A cash-drawer session: opening float, everything that went through the till, and the count at close."""
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='opened_shifts')
    opened_at = models.DateTimeField(auto_now_add=True)
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='closed_shifts')
    closed_at = models.DateTimeField(null=True, blank=True)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    @property
    def number(self): return f'SH-{self.pk:04d}'
    @property
    def is_open(self): return self.closed_at is None
    @property
    def variance(self): return None if self.counted_cash is None or self.expected_cash is None else self.counted_cash - self.expected_cash


class ShiftMovement(models.Model):
    """Cash moved in or out of the drawer outside of sales: safe drops, petty-cash payouts, float top-ups."""
    KINDS = [('drop', 'Cash drop to safe/bank'), ('payout', 'Petty cash payout'), ('float', 'Float added')]
    shift = models.ForeignKey(Shift, related_name='movements', on_delete=models.CASCADE)
    kind = models.CharField(max_length=10, choices=KINDS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=200)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='positive_shift_movement')]


class StockLevel(models.Model):
    """On-hand and reserved quantity of one product at one location. Product.stock/reserved are the totals."""
    product = models.ForeignKey(Product, related_name='levels', on_delete=models.CASCADE)
    location = models.ForeignKey(Location, related_name='levels', on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    @property
    def available(self): return self.quantity - self.reserved
    class Meta:
        constraints = [models.UniqueConstraint(fields=['product', 'location'], name='one_level_per_location'), models.CheckConstraint(condition=models.Q(quantity__gte=models.F('reserved')), name='level_reserved_within_quantity')]


class StockTransfer(models.Model):
    """Goods moving between locations: dispatched out of the source, then received (possibly in parts) at the destination."""
    STATUSES = [('Draft', 'Draft'), ('Dispatched', 'Dispatched'), ('Partially received', 'Partially received'), ('Received', 'Received'), ('Cancelled', 'Cancelled')]
    source = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='transfers_out')
    destination = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='transfers_in')
    status = models.CharField(max_length=20, choices=STATUSES, default='Draft')
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='+')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def number(self): return f'TR-{self.pk:05d}'


class StockTransferLine(models.Model):
    transfer = models.ForeignKey(StockTransfer, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    received_quantity = models.PositiveIntegerField(default=0)
    written_off = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(received_quantity__lte=models.F('quantity')), name='transfer_received_within_sent')]


class StockCount(models.Model):
    """A physical count at one location: expected quantities are snapshotted at start, variances posted at completion."""
    STATUSES = [('Open', 'Open'), ('Completed', 'Completed'), ('Cancelled', 'Cancelled')]
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='counts')
    status = models.CharField(max_length=10, choices=STATUSES, default='Open')
    category = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='+')
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def number(self): return f'SC-{self.pk:05d}'


class StockCountLine(models.Model):
    count = models.ForeignKey(StockCount, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    expected = models.IntegerField()
    counted = models.IntegerField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['count', 'product'], name='one_count_line_per_product')]
