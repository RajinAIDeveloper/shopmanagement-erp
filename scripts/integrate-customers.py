from pathlib import Path

def edit(path, old, new):
    p=Path(path);s=p.read_text(encoding='utf-8')
    if old not in s: raise RuntimeError(f'Missing anchor: {path}: {old[:80]}')
    p.write_text(s.replace(old,new),encoding='utf-8')

edit('config/settings.py',"STORE_THROTTLES = {", "STORE_THROTTLES = {\n    'customer_auth': (12, 900), 'quote': (120, 60),")
edit('store/views.py',"    return JsonResponse({**commerce_data(), 'wishlist':list(Product.objects.filter(pk__in=request.session.get('wishlist',[]),active=True).values_list('pk',flat=True)),", "    from .customers import wishlist_ids, storefront_data\n    return JsonResponse({**commerce_data(), **storefront_data(request), 'wishlist':wishlist_ids(request),")
edit('store/views.py', "            customer = Customer.objects.create(name=required(d,'name'), phone=phone_number(d), address=required(d,'address'), email=str(d.get('email',''))[:254])\n            order = create_order(d['items'],'Online','COD',customer)", """            from .customers import customer_for, quote, coupon_identity, loyalty_post
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
            if q['points']: loyalty_post(customer,order,-q['points'],f'Redeemed on {order.number}',f'redeem:{order.pk}')""")
edit('store/services.py',"        journal(order.number, 'Sales revenue', 'VAT payable', order.tax_total)","        journal(order.number, 'Sales revenue', 'VAT payable', order.tax_total)\n        journal(order.number, 'Sales revenue', 'Shipping revenue', order.shipping_total-order.shipping_tax)")
edit('store/services.py',"    order.save(update_fields=['status','reservation_expires_at'])\n    return order", "    order.save(update_fields=['status','reservation_expires_at'])\n    from .customers import loyalty_sync, release_promotions\n    if status == 'Cancelled': release_promotions(order)\n    if status == 'Delivered': loyalty_sync(order)\n    return order")
edit('store/services.py',"    if not order: return False\n", "    if not order: return False\n    from .customers import release_promotions\n    release_promotions(order)\n")
edit('store/selling.py',"    factor = order.total / gross if gross else Decimal('0')", "    factor = (order.total-order.shipping_total) / gross if gross else Decimal('0')")
edit('store/selling.py',"    if fully_returned: total = order.total - order.returned_total", "    if fully_returned: total = order.total - order.shipping_total - order.returned_total")
edit('store/selling.py',"    sale_return.refund = refund", "    from .customers import loyalty_sync, loyalty_post\n    loyalty_sync(order)\n    if order.customer_id and order.loyalty_redeemed:\n        restored = order.customer.loyalty_entries.filter(event__startswith=f'return-points:{order.pk}:').aggregate(total=Sum('points'))['total'] or 0\n        desired = order.loyalty_redeemed if fully_returned else int(order.loyalty_redeemed * order.returned_total / (order.total-order.shipping_total)) if order.total>order.shipping_total else 0\n        loyalty_post(order.customer,order,desired-restored,f'Returned items {sale_return.number}',f'return-points:{order.pk}:{sale_return.pk}')\n    sale_return.refund = refund")
edit('store/selling.py',"    return order\n\n\n# --- Returns", "    from .customers import loyalty_sync\n    if channel == 'POS': loyalty_sync(order)\n    return order\n\n\n# --- Returns")
edit('store/commerce.py',"    return JsonResponse({'reviews':list(Review.objects.filter(product_id=pk,status='approved').values('id','author','rating','title','body','created_at')[:100])})", "    return JsonResponse({'reviews':list(Review.objects.filter(product_id=pk,status='approved').values('id','author','rating','title','body','created_at','verified_purchase')[:100])})")
edit('store/commerce.py',"        visitor=request.session.get('review_visitor') or str(uuid.uuid4())", "        from .customers import customer_for\n        from .models import Order\n        customer=customer_for(request)\n        visitor=str(uuid.uuid5(uuid.NAMESPACE_URL,f'altab-customer:{customer.pk}')) if customer else request.session.get('review_visitor') or str(uuid.uuid4())\n        verified=bool(customer and Order.objects.filter(customer=customer,status__in=['Completed','Delivered','Partially returned'],lines__product=product).exists())")
edit('store/commerce.py',"body=text(d,'body',2000,True),visitor=visitor)","body=text(d,'body',2000,True),visitor=visitor,customer=customer,verified_purchase=verified)")
edit('store/commerce.py',"        saved=list(Product.objects.filter(pk__in=request.session.get('wishlist',[]),active=True).values_list('pk',flat=True))", "        from .customers import wishlist_ids, customer_for\n        from .models import WishlistItem\n        saved=wishlist_ids(request)")
edit('store/commerce.py',"        request.session['wishlist']=saved\n", "        customer=customer_for(request)\n        if customer:\n            if d['saved']: WishlistItem.objects.get_or_create(customer=customer,product_id=pk)\n            else: WishlistItem.objects.filter(customer=customer,product_id=pk).delete()\n        request.session['wishlist']=saved\n")
