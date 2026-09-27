from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from store import views
from store import commerce
from store import operations
from store import security
from store import selling
from store import inventory
from store import accounting
from store import customers
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', security.ThrottledLoginView.as_view()),
    path('logout/', auth_views.LogoutView.as_view()),
    path('healthz/', security.health),
    path('', views.workspace),
    path('shop/', views.shop),
    path('api/state/', views.state),
    path('api/action/', views.action),
    path('api/catalog/', views.catalog),
    path('api/checkout/', views.checkout),
    path('api/track/', views.track),
    path('receipt/<int:pk>/', views.receipt),
    path('api/export/', views.export),
    path('api/content/', commerce.manage),
    path('api/upload/', commerce.upload),
    path('api/reviews/<int:pk>/', commerce.reviews),
    path('api/reviews/', commerce.submit_review),
    path('api/wishlist/', commerce.wishlist),
    path('api/products/save/', operations.product_save),
    path('api/settlements/', operations.settlements),
    path('api/settlements/save/', operations.settlement_save),
    path('api/activity/', operations.activity),
    path('api/returns/', selling.returns),
    path('api/shifts/', selling.shifts),
    path('api/shifts/action/', selling.shift_action),
    path('api/customers/<int:pk>/', selling.customer_account),
    path('api/inventory/', inventory.overview),
    path('api/inventory/action/', inventory.action),
    path('api/accounting/', accounting.overview),
    path('api/accounting/action/', accounting.action),
    path('api/account/', customers.account),
    path('api/account/action/', customers.account_action),
    path('api/checkout/quote/', customers.checkout_quote),
    path('api/crm/', customers.manage_data),
    path('api/crm/action/', customers.manage_action),
    path('customer/<int:pk>/statement/', customers.statement),
    path('sw.js', views.service_worker),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
