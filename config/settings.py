"""Altab Tech settings. Everything deployment-specific is read from the environment; see README → Deployment."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=''): return os.environ.get(name, default)
def env_flag(name, default='0'): return env(name, default).lower() in ['1', 'true', 'yes', 'on']
def env_list(name, default=''): return [item.strip() for item in env(name, default).split(',') if item.strip()]


DEBUG = env_flag('DJANGO_DEBUG', '1')
TESTING = len(sys.argv) > 1 and sys.argv[1] == 'test'
SECRET_KEY = env('DJANGO_SECRET_KEY', 'local-development-only-altab-tech')
if not DEBUG and SECRET_KEY == 'local-development-only-altab-tech':
    raise RuntimeError('Set DJANGO_SECRET_KEY before running in production.')
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver')
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')

INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'store']
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'store.commerce.branding']}}]
WSGI_APPLICATION = 'config.wsgi.application'

# Database: SQLite by default (WAL + IMMEDIATE transactions so concurrent writers queue instead of failing),
# or PostgreSQL when DJANGO_DB_ENGINE=postgresql.
if env('DJANGO_DB_ENGINE', 'sqlite3') == 'postgresql':
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env('DJANGO_DB_NAME', 'altab'), 'USER': env('DJANGO_DB_USER', 'altab'), 'PASSWORD': env('DJANGO_DB_PASSWORD'),
        'HOST': env('DJANGO_DB_HOST', 'localhost'), 'PORT': env('DJANGO_DB_PORT', '5432'),
        'CONN_MAX_AGE': int(env('DJANGO_DB_CONN_MAX_AGE', '60')), 'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {'sslmode': env('DJANGO_DB_SSLMODE', 'prefer')},
    }}
else:
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': env('DJANGO_DB_PATH', str(BASE_DIR / 'db.sqlite3')),
        'OPTIONS': {'timeout': 20, 'transaction_mode': 'IMMEDIATE', 'init_command': 'PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;'},
    }}

# Cache backs rate limiting. The in-process default is fine for one worker; point several workers at Redis.
if env('DJANGO_CACHE_URL'):
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': env('DJANGO_CACHE_URL')}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'altab-tech'}}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Dhaka'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = Path(env('DJANGO_STATIC_ROOT', str(BASE_DIR / 'staticfiles')))
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(env('DJANGO_MEDIA_ROOT', str(BASE_DIR / 'media')))
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    # Hashed, compressed static files in production; plain finders while developing or testing.
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage' if not DEBUG and not TESTING else 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG
FILE_UPLOAD_PERMISSIONS = 0o644
DATA_UPLOAD_MAX_MEMORY_SIZE = 4 * 1024 * 1024
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
CSRF_FAILURE_VIEW = 'store.security.csrf_failure'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = int(env('DJANGO_SESSION_HOURS', '12')) * 3600
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CONTENT_TYPE_NOSNIFF = True
STORE_BEHIND_PROXY = env_flag('DJANGO_BEHIND_PROXY')
if STORE_BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    USE_X_FORWARDED_HOST = True
# DJANGO_HTTPS=0 is only for a LAN-only counter behind a firewall with no TLS; everything else assumes HTTPS.
STORE_HTTPS = env_flag('DJANGO_HTTPS', '1')
if not DEBUG and STORE_HTTPS:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_flag('DJANGO_SSL_REDIRECT', '1') and not TESTING
    SECURE_HSTS_SECONDS = int(env('DJANGO_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_flag('DJANGO_HSTS_SUBDOMAINS', '1')
    SECURE_HSTS_PRELOAD = env_flag('DJANGO_HSTS_PRELOAD')

# HSTS preload is a one-way door for the whole domain; opt in with DJANGO_HSTS_PRELOAD=1 rather than by default.
SILENCED_SYSTEM_CHECKS = ['security.W021']

# Abuse limits per client address: (requests, window seconds). Login counts failed attempts only.
STORE_THROTTLES = {
    'customer_auth': (12, 900), 'quote': (120, 60),
    'login': (int(env('STORE_LOGIN_ATTEMPTS', '8')), 15 * 60),
    'checkout': (int(env('STORE_CHECKOUTS_PER_10_MIN', '10')), 10 * 60),
    'review': (5, 60 * 60),
    'wishlist': (60, 60),
    'track': (60, 60),
    'upload': (30, 60 * 60),
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'root': {'handlers': ['console'], 'level': env('DJANGO_LOG_LEVEL', 'INFO')},
    'loggers': {
        'django.request': {'level': 'ERROR' if TESTING else 'WARNING', 'propagate': True},
        'django.security': {'level': 'ERROR' if TESTING else 'WARNING', 'propagate': True},
        'store': {'level': 'ERROR' if TESTING else env('DJANGO_LOG_LEVEL', 'INFO'), 'propagate': True},
    },
}
