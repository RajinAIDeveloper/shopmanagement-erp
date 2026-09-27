from django.contrib import admin
from .models import Product, Supplier, Customer, Order, StockMovement, Purchase, Expense, JournalEntry, AuditLog, StoreProfile, CategoryAttribute, Offer, Announcement, Review, Settlement, OrderPayment, SaleReturn, SaleReturnLine, CreditTransaction, Shift, ShiftMovement, Location, StaffProfile, StockLevel, StockTransfer, StockTransferLine, StockCount, StockCountLine, PurchaseLine, PurchaseReceipt, PurchaseReceiptLine, PurchaseReturn, PurchaseReturnLine


@admin.register(Product, Supplier, Customer, Order, StockMovement, Purchase, Expense, JournalEntry, AuditLog, StoreProfile, CategoryAttribute, Offer, Announcement, Review, Settlement, OrderPayment, SaleReturn, SaleReturnLine, CreditTransaction, Shift, ShiftMovement, Location, StaffProfile, StockLevel, StockTransfer, StockTransferLine, StockCount, StockCountLine, PurchaseLine, PurchaseReceipt, PurchaseReceiptLine, PurchaseReturn, PurchaseReturnLine)
class ReadOnlyRecordAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
