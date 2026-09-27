"""Dated, immutable postings and financial reporting over the operational ledger."""
import csv
import io
import json
import uuid
from datetime import date
from decimal import Decimal
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction, IntegrityError
from django.db.models import Q, Sum
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from .models import Account, FiscalPeriod, JournalEntry, JournalVoucher, BankReconciliation, BankStatementLine, StoreProfile, AuditLog, Order, Purchase
from .operations import finance_access
from .selling import manager_access, ACTIVE_SALE_STATUSES
from .services import money, integer
from .commerce import text

SYSTEM_ACCOUNTS = {
    'Cash': ('1000', 'asset', True, 'operating'),
    'Bank / mobile payments': ('1010', 'asset', True, 'operating'),
    'Accounts receivable': ('1100', 'asset', False, 'operating'),
    'Inventory': ('1200', 'asset', False, 'operating'),
    'Accounts payable': ('2000', 'liability', False, 'operating'),
    'VAT payable': ('2100', 'liability', False, 'operating'),
    'Customer refunds payable': ('2200', 'liability', False, 'operating'),
    'Store credit liability': ('2300', 'liability', False, 'operating'),
    'Customer advances': ('2400', 'liability', False, 'operating'),
    'Opening equity': ('3000', 'equity', False, 'financing'),
    'Sales revenue': ('4000', 'income', False, 'operating'),
    'Inventory gain': ('4100', 'income', False, 'operating'),
    'Cost of goods sold': ('5000', 'expense', False, 'operating'),
    'Operating expenses': ('5100', 'expense', False, 'operating'),
    'Inventory loss': ('5200', 'expense', False, 'operating'),
    'Cash over / short': ('5300', 'expense', False, 'operating'),
    'Customer goodwill': ('5400', 'expense', False, 'operating'),
    'Courier fees': ('5500', 'expense', False, 'operating'),
    'Shipping revenue': ('4200', 'income', False, 'operating'),
}
CONTROL_ACCOUNTS = {'Accounts receivable', 'Accounts payable', 'Inventory', 'Customer refunds payable', 'Store credit liability', 'Customer advances'}


def parsed_date(value):
    try: return date.fromisoformat(str(value))
    except (ValueError, TypeError): raise ValueError('Enter a date in YYYY-MM-DD format.')


def signed_money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal('.01'))
        if not result.is_finite() or abs(result) > Decimal('999999999'): raise ValueError()
        return result
    except Exception: raise ValueError('Enter a valid amount.')


def ensure_accounts():
    for name, (code, kind, cash, flow) in SYSTEM_ACCOUNTS.items():
        Account.objects.get_or_create(name=name, defaults={'code': code, 'kind': kind, 'cash': cash, 'cash_flow': flow, 'system': True})


def assert_open(day):
    # Serialize closing and posting on the same row on PostgreSQL as well as SQLite.
    StoreProfile.current()
    StoreProfile.objects.select_for_update().get(pk=1)
    if FiscalPeriod.objects.filter(start__lte=day, end__gte=day, closed=True).exists():
        raise ValueError('This fiscal period is closed. Reopen it before posting.')


@transaction.atomic
def post_pair(reference, debit, credit, amount, day=None, voucher=None):
    amount = money(amount)
    if not amount: return None
    day = day or timezone.localdate()
    assert_open(day)
    if debit == credit: raise ValueError('Debit and credit accounts must differ.')
    for name in (debit, credit):
        account = Account.objects.filter(name=name).first()
        if account is None and name in SYSTEM_ACCOUNTS:
            code, kind, cash, flow = SYSTEM_ACCOUNTS[name]
            account = Account.objects.create(name=name, code=code, kind=kind, cash=cash, cash_flow=flow, system=True)
        if account is None or not account.active: raise ValueError(f'Account is unavailable: {name}')
    return JournalEntry.objects.create(reference=reference[:150], debit=debit, credit=credit, amount=amount, effective_date=day, voucher=voucher)


def account_balances(entries):
    balances = {}
    for side, sign in [('debit', 1), ('credit', -1)]:
        for row in entries.values(side).annotate(total=Sum('amount')):
            balances[row[side]] = balances.get(row[side], Decimal(0)) + sign * row['total']
    return balances


def financial_report(start, end):
    if start > end: raise ValueError('Start date must precede end date.')
    accounts = list(Account.objects.all())
    all_to_end = JournalEntry.objects.filter(effective_date__lte=end)
    period = all_to_end.filter(effective_date__gte=start)
    closing = account_balances(all_to_end)
    activity = account_balances(period)
    opening = account_balances(all_to_end.filter(effective_date__lt=start))
    trial = [{'account': a.name, 'code': a.code, 'kind': a.kind, 'opening': opening.get(a.name, 0), 'movement': activity.get(a.name, 0), 'closing': closing.get(a.name, 0), 'debit': max(0, closing.get(a.name, 0)), 'credit': max(0, -closing.get(a.name, 0))} for a in accounts]
    income = -sum(activity.get(a.name, 0) for a in accounts if a.kind == 'income')
    expense = sum(activity.get(a.name, 0) for a in accounts if a.kind == 'expense')
    assets = sum(closing.get(a.name, 0) for a in accounts if a.kind == 'asset')
    liabilities = -sum(closing.get(a.name, 0) for a in accounts if a.kind == 'liability')
    equity = -sum(closing.get(a.name, 0) for a in accounts if a.kind == 'equity')
    retained = -sum(closing.get(a.name, 0) for a in accounts if a.kind in ['income', 'expense'])
    cash_names = {a.name for a in accounts if a.cash}
    by_name = {a.name: a for a in accounts}
    flows = {k: Decimal(0) for k in ['operating', 'investing', 'financing']}
    for row in period.filter(Q(debit__in=cash_names) | Q(credit__in=cash_names)):
        if row.debit in cash_names and row.credit in cash_names: continue
        other = row.credit if row.debit in cash_names else row.debit
        kind = by_name[other].cash_flow if other in by_name else 'operating'
        flows[kind] += row.amount if row.debit in cash_names else -row.amount
    return {'start': start, 'end': end, 'trial': trial, 'income': income, 'expenses': expense, 'profit': income-expense,
            'assets': assets, 'liabilities': liabilities, 'equity': equity, 'retained_earnings': retained,
            'balance_difference': assets-liabilities-equity-retained, 'cash_flow': flows,
            'cash_opening': sum(opening.get(n, 0) for n in cash_names), 'cash_closing': sum(closing.get(n, 0) for n in cash_names),
            'vat_movement': -activity.get('VAT payable', 0), 'vat_due': -closing.get('VAT payable', 0)}


def aging():
    today = timezone.localdate()
    rows = []
    for o in Order.objects.filter(status__in=ACTIVE_SALE_STATUSES).select_related('customer'):
        if o.due:
            days = (today-timezone.localdate(o.created_at)).days
            rows.append({'kind': 'receivable', 'document': o.number, 'party': o.customer.name if o.customer else 'Walk-in', 'due': o.due, 'days': days, 'bucket': '0–30' if days <= 30 else '31–60' if days <= 60 else '61–90' if days <= 90 else '90+'})
    for p in Purchase.objects.exclude(status='Cancelled').select_related('supplier').prefetch_related('lines'):
        due = p.payable-p.paid_total
        if due:
            days = (today-timezone.localdate(p.created_at)).days
            rows.append({'kind': 'payable', 'document': p.number, 'party': p.supplier.name, 'due': due, 'days': days, 'bucket': '0–30' if days <= 30 else '31–60' if days <= 60 else '61–90' if days <= 90 else '90+'})
    return rows


def reconciliation_data(rec):
    lines = list(rec.lines.select_related('entry'))
    used = BankStatementLine.objects.filter(account=rec.account, entry__isnull=False).values_list('entry_id', flat=True)
    entries = JournalEntry.objects.filter(effective_date__range=(rec.start, rec.end)).filter(Q(debit=rec.account.name) | Q(credit=rec.account.name)).exclude(pk__in=used)
    return {'id': rec.pk, 'account': rec.account_id, 'account_name': rec.account.name, 'start': rec.start, 'end': rec.end, 'opening': rec.opening, 'closing': rec.closing, 'reference': rec.reference, 'status': rec.status,
            'difference': rec.closing-rec.opening-sum((l.amount for l in lines), Decimal(0)),
            'lines': [{'id': l.pk, 'date': l.date, 'description': l.description, 'amount': l.amount, 'entry': l.entry_id} for l in lines],
            'unmatched': [{'id': e.pk, 'date': e.effective_date, 'reference': e.reference, 'amount': e.amount if e.debit == rec.account.name else -e.amount} for e in entries]}


@require_GET
@never_cache
def overview(request):
    if not finance_access(request.user): return JsonResponse({'error': 'Accounting permission required.'}, status=403)
    try:
        end = parsed_date(request.GET.get('end', timezone.localdate()))
        start = parsed_date(request.GET.get('start', end.replace(day=1)))
        report = financial_report(start, end)
        if request.GET.get('format') == 'csv':
            stream = io.StringIO(); writer = csv.writer(stream)
            writer.writerow(['Code', 'Account', 'Type', 'Opening', 'Movement', 'Closing', 'Debit', 'Credit'])
            for r in report['trial']: writer.writerow([r[k] for k in ['code', 'account', 'kind', 'opening', 'movement', 'closing', 'debit', 'credit']])
            response = HttpResponse(stream.getvalue(), content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="trial-balance.csv"'
            return response
        vouchers = JournalVoucher.objects.select_related('staff', 'reversal').prefetch_related('entries').order_by('-pk')[:100]
        data = {'report': report, 'accounts': list(Account.objects.values()), 'periods': list(FiscalPeriod.objects.values()), 'aging': aging(),
                'vouchers': [{'id': v.pk, 'reference': v.reference, 'memo': v.memo, 'date': v.date, 'staff': v.staff.username, 'reverses': v.reverses_id, 'reversed': hasattr(v, 'reversal'), 'entries': list(v.entries.values('debit', 'credit', 'amount'))} for v in vouchers],
                'reconciliations': list(BankReconciliation.objects.order_by('-pk').values('id', 'account__name', 'start', 'end', 'status', 'reference')[:100])}
        if request.GET.get('reconciliation'): data['reconciliation'] = reconciliation_data(BankReconciliation.objects.select_related('account').get(pk=integer(request.GET['reconciliation'])))
        return JsonResponse(data)
    except (ValueError, ObjectDoesNotExist) as exc: return JsonResponse({'error': str(exc)}, status=400)


@transaction.atomic
def save_voucher(d, user):
    token = uuid.UUID(str(d.get('request_id')))
    previous = JournalVoucher.objects.filter(request_id=token).first()
    if previous:
        if previous.staff_id != user.pk or previous.reference != text(d, 'reference', 150, True): raise ValueError('Request ID is already in use.')
        return previous
    day = parsed_date(d.get('date'))
    if day > timezone.localdate(): raise ValueError('Future-dated postings are not allowed.')
    assert_open(day)
    reverse = None
    if d.get('reverses'):
        reverse = JournalVoucher.objects.select_for_update().get(pk=integer(d['reverses']))
        if reverse.reverses_id or JournalVoucher.objects.filter(reverses=reverse).exists(): raise ValueError('Voucher already reversed or is a reversal.')
        pairs = [(e.credit, e.debit, e.amount) for e in reverse.entries.all()]
    else:
        lines = d.get('lines', [])
        if not isinstance(lines, list) or not 2 <= len(lines) <= 100: raise ValueError('Provide between 2 and 100 journal lines.')
        sides = {'debit': [], 'credit': []}
        for line in lines:
            account = Account.objects.get(pk=integer(line.get('account')), active=True)
            if account.name in CONTROL_ACCOUNTS: raise ValueError('Use the stock, sales or settlement workflow for control accounts.')
            debit, credit = money(line.get('debit', 0)), money(line.get('credit', 0))
            if bool(debit) == bool(credit): raise ValueError('Each line needs either a debit or a credit.')
            sides['debit' if debit else 'credit'].append([account.name, debit or credit])
        if sum(a[1] for a in sides['debit']) != sum(a[1] for a in sides['credit']): raise ValueError('Total debits must equal total credits.')
        if {a[0] for a in sides['debit']} & {a[0] for a in sides['credit']}: raise ValueError('An account cannot appear on both sides of a voucher.')
        pairs = []
        debits, credits = sides['debit'], sides['credit']
        while debits and credits:
            amount = min(debits[0][1], credits[0][1]); pairs.append((debits[0][0], credits[0][0], amount))
            debits[0][1] -= amount; credits[0][1] -= amount
            if not debits[0][1]: debits.pop(0)
            if not credits[0][1]: credits.pop(0)
    v = JournalVoucher.objects.create(reference=text(d, 'reference', 150, True), memo=text(d, 'memo', 500, True), date=day, request_id=token, staff=user, reverses=reverse)
    for debit, credit, amount in pairs: post_pair(v.reference, debit, credit, amount, day, v)
    return v


@transaction.atomic
def perform(d, user):
    kind = d.get('action')
    if kind == 'account':
        if not manager_access(user): raise ValueError('Manager permission required.')
        a = Account.objects.get(pk=integer(d['id'])) if d.get('id') else Account()
        if a.pk and (a.system or JournalEntry.objects.filter(Q(debit=a.name) | Q(credit=a.name)).exists()): raise ValueError('Used and system accounts cannot be changed; create another account.')
        a.name, a.code = text(d, 'name', 80, True), text(d, 'code', 20, True)
        a.kind, a.cash_flow = d.get('kind'), d.get('cash_flow', 'operating')
        if a.kind not in dict(Account.TYPES) or a.cash_flow not in ['operating', 'investing', 'financing']: raise ValueError('Invalid account classification.')
        a.cash = d.get('cash') is True
        if a.cash and a.kind != 'asset': raise ValueError('Cash accounts must be assets.')
        a.save(); result = {'id': a.pk}
    elif kind == 'voucher': result = {'id': save_voucher(d, user).pk}
    elif kind == 'period':
        if not manager_access(user): raise ValueError('Manager permission required.')
        assert_lock = StoreProfile.current(); StoreProfile.objects.select_for_update().get(pk=assert_lock.pk)
        if d.get('id'):
            p = FiscalPeriod.objects.select_for_update().get(pk=integer(d['id']))
            p.closed = d.get('closed') is True
            if p.closed and financial_report(p.start, p.end)['balance_difference']: raise ValueError('Resolve the trial balance difference before closing.')
            p.closed_by = user; p.closed_at = timezone.now() if p.closed else None
        else:
            start, end = parsed_date(d.get('start')), parsed_date(d.get('end'))
            if end < start or FiscalPeriod.objects.filter(start__lte=end, end__gte=start).exists(): raise ValueError('Fiscal dates must be ordered and may not overlap.')
            p = FiscalPeriod(name=text(d, 'name', 80, True), start=start, end=end)
        p.save(); result = {'id': p.pk}
    elif kind == 'reconciliation':
        account = Account.objects.get(pk=integer(d.get('account')), cash=True, active=True)
        start, end = parsed_date(d.get('start')), parsed_date(d.get('end'))
        if start > end: raise ValueError('Invalid statement dates.')
        if BankReconciliation.objects.filter(account=account, start__lte=end, end__gte=start).exists(): raise ValueError('Statement periods cannot overlap for an account.')
        rec = BankReconciliation.objects.create(account=account, start=start, end=end, opening=signed_money(d.get('opening')), closing=signed_money(d.get('closing')), reference=text(d, 'reference', 150, True), staff=user)
        lines = d.get('lines', [])
        if not isinstance(lines, list) or not 1 <= len(lines) <= 1000: raise ValueError('Provide between 1 and 1000 statement lines.')
        for line in lines:
            day = parsed_date(line.get('date')); amount = signed_money(line.get('amount'))
            if not start <= day <= end or not amount: raise ValueError('Statement line date or amount is invalid.')
            BankStatementLine.objects.create(reconciliation=rec, account=account, date=day, amount=amount, description=text(line, 'description', 200, True))
        result = {'id': rec.pk}
    elif kind in ['match', 'reconcile']:
        rec = BankReconciliation.objects.select_for_update().select_related('account').get(pk=integer(d.get('id')))
        if rec.status != 'Draft': raise ValueError('Completed statements cannot be changed.')
        if kind == 'match':
            line = rec.lines.get(pk=integer(d.get('line')))
            entry = JournalEntry.objects.get(pk=integer(d['entry'])) if d.get('entry') else None
            if entry:
                amount = entry.amount if entry.debit == rec.account.name else -entry.amount if entry.credit == rec.account.name else None
                if amount != line.amount or not rec.start <= entry.effective_date <= rec.end: raise ValueError('The journal amount, account and date must match this statement.')
            line.entry = entry; line.save(update_fields=['entry'])
        else:
            data = reconciliation_data(rec)
            if data['difference'] or rec.lines.filter(entry=None).exists(): raise ValueError('Match every statement line and resolve the closing balance difference.')
            rec.status = 'Completed'; rec.save(update_fields=['status'])
        result = {'id': rec.pk}
    else: raise ValueError('Unknown accounting action.')
    AuditLog.objects.create(staff=user, action=f'Accounting {kind}', detail=json.dumps({**d, **result}, default=str))
    return result


@require_POST
def action(request):
    if not finance_access(request.user): return JsonResponse({'error': 'Accounting permission required.'}, status=403)
    try:
        d = json.loads(request.body)
        if not isinstance(d, dict): raise ValueError('Expected an object.')
        return JsonResponse({'ok': True, **perform(d, request.user)})
    except (ValueError, TypeError, KeyError, ObjectDoesNotExist) as exc: return JsonResponse({'error': str(exc)}, status=400)
    except IntegrityError: return JsonResponse({'error': 'This account, request ID or statement match already exists.'}, status=409)
