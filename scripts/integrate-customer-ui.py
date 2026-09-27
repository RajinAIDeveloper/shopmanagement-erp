from pathlib import Path
import json
for name in ['shop','workspace']:
    p=Path(f'templates/{name}.html');s=p.read_text(encoding='utf-8').replace('</head>','<link rel="stylesheet" href="{% static \'customers.css\' %}"></head>').replace('</body>','<script src="{% static \'customers.js\' %}"></script></body>');p.write_text(s,encoding='utf-8')
p=Path('package.json');d=json.loads(p.read_text());d['scripts']['check']+=' && node --check static/customers.js';d['scripts']['test:customers']='node scripts/customers-smoke.cjs';p.write_text(json.dumps(d,indent=2)+'\n')
p=Path('store/views.py');s=p.read_text(encoding='utf-8').replace("'pricing_tier':c.pricing_tier", "'notes':c.notes,'tags':c.tags,'birthday':c.birthday,'loyalty_points':c.loyalty_points,'pricing_tier':c.pricing_tier")
old="    if not request.user.is_staff: return HttpResponse(status=403)\n    return render(request,'receipt.html',{'order':get_object_or_404(Order.objects.select_related('customer').prefetch_related('lines__product'),pk=pk)})"
new="    order=get_object_or_404(Order.objects.select_related('customer').prefetch_related('lines__product'),pk=pk)\n    if not request.user.is_staff and (not order.customer_id or order.customer.user_id!=request.user.pk): return HttpResponse(status=403)\n    return render(request,'receipt.html',{'order':order})"
assert old in s;s=s.replace(old,new);p.write_text(s,encoding='utf-8')
p=Path('static/commerce.js');s=p.read_text(encoding='utf-8').replace('${esc(r.author)}</strong>','${esc(r.author)}${r.verified_purchase?\' · Verified purchase\':\'\'}</strong>');p.write_text(s,encoding='utf-8')
