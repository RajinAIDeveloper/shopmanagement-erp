"""Rate limiting, sign-in auditing, JSON-friendly CSRF failures, and the health check."""
import logging
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.cache import cache
from django.db import connection, DatabaseError
from django.dispatch import receiver
from django.http import JsonResponse
from django.shortcuts import render
from django.views import csrf as csrf_views
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from .models import AuditLog

logger = logging.getLogger('store')


def client_ip(request):
    """The peer address, or the first X-Forwarded-For hop when a trusted proxy is declared."""
    if settings.STORE_BEHIND_PROXY:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded: return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def throttle_key(request, scope): return f'throttle:{scope}:{client_ip(request)}'


def throttle_count(request, scope):
    return cache.get(throttle_key(request, scope), 0)


def throttle_hit(request, scope):
    """Count one attempt within the scope's window and return the new total."""
    limit, window = settings.STORE_THROTTLES[scope]
    key = throttle_key(request, scope)
    try:
        cache.add(key, 0, window)
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, window)
        return 1


def throttled(request, scope):
    """True when this client has exhausted the scope's allowance; every call counts as an attempt."""
    limit, window = settings.STORE_THROTTLES[scope]
    exceeded = throttle_hit(request, scope) > limit
    if exceeded: logger.warning('Rate limit reached: scope=%s ip=%s', scope, client_ip(request))
    return exceeded


def throttled_response(message='Too many requests. Please wait a moment and try again.'):
    response = JsonResponse({'error': message}, status=429)
    response['Retry-After'] = '60'
    return response


def csrf_failure(request, reason=''):
    if request.path.startswith('/api/'):
        return JsonResponse({'error': 'CSRF verification failed. Reload the page and try again.'}, status=403)
    return csrf_views.csrf_failure(request, reason)


class ThrottledLoginView(auth_views.LoginView):
    """Standard Django sign-in that refuses further attempts after repeated failures from one address."""
    template_name = 'login.html'

    def post(self, request, *args, **kwargs):
        limit = settings.STORE_THROTTLES['login'][0]
        if throttle_count(request, 'login') >= limit:
            return render(request, self.template_name, {'form': self.get_form_class()(request), 'throttled': True, 'next': request.POST.get('next', '')}, status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        throttle_hit(self.request, 'login')
        return super().form_invalid(form)


def request_summary(request):
    if request is None: return ''
    agent = request.META.get('HTTP_USER_AGENT', '')[:160]
    return f'{client_ip(request)} · {agent}'


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs):
    AuditLog.objects.create(staff=user, action='Signed in', detail=request_summary(request))


@receiver(user_logged_out)
def record_logout(sender, request, user, **kwargs):
    if user is not None and user.is_authenticated:
        AuditLog.objects.create(staff=user, action='Signed out', detail=request_summary(request))


@receiver(user_login_failed)
def record_login_failure(sender, credentials, request, **kwargs):
    username = str(credentials.get('username', ''))[:150]
    AuditLog.objects.create(action='Sign-in failed', detail=f'username={username} · {request_summary(request)}')


@require_GET
@never_cache
def health(request):
    """Liveness/readiness probe for load balancers; reports only whether the database answers."""
    try:
        with connection.cursor() as cursor: cursor.execute('SELECT 1')
        return JsonResponse({'status': 'ok', 'database': 'ok'})
    except DatabaseError:
        logger.exception('Health check database failure')
        return JsonResponse({'status': 'degraded', 'database': 'unavailable'}, status=503)
