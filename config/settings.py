"""
Django settings for the westridgeApp_mrp (MRP purchasing planner) project.

All secrets and environment-specific values come from environment variables,
optionally loaded from a gitignored .env file in the project root.
"""
from pathlib import Path
import os
import sys

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env (never committed)
load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


DEBUG = env_bool('DEBUG', False)

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'dev-only-insecure-key'
    else:
        raise ImproperlyConfigured('SECRET_KEY must be set when DEBUG is off.')

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')


# Application definition

INSTALLED_APPS = [
    'mrp.apps.MrpConfig',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.auth.middleware.LoginRequiredMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database: own PostgreSQL database, separate from westridgeApp.
# DATABASE_URL e.g. postgres://mrp_user:password@localhost:5432/mrp
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    DATABASES = {'default': dj_database_url.parse(DATABASE_URL, conn_max_age=60)}
elif DEBUG:
    # Local development / tests without Postgres
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'dev.sqlite3',
        }
    }
else:
    raise ImproperlyConfigured('DATABASE_URL must be set when DEBUG is off.')


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'mrp:plan'
LOGOUT_REDIRECT_URL = 'login'


# Internationalization (pacific time, same as westridgeApp)
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Los_Angeles'
USE_I18N = True
USE_TZ = True


# Static files. Under Apache the app is mounted at /mrp, so statics live at /mrp/static/.
STATIC_URL = os.environ.get('STATIC_URL', '/static/')
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Cookies: this app shares a host with westridgeApp, so keep names and paths separate.
SESSION_COOKIE_NAME = 'mrp_sessionid'
CSRF_COOKIE_NAME = 'mrp_csrftoken'
SESSION_COOKIE_PATH = os.environ.get('COOKIE_PATH', '/')
CSRF_COOKIE_PATH = SESSION_COOKIE_PATH
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = env_bool('SECURE_COOKIES', False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True


# Email (Fastmail SMTP with an app-specific password)
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.fastmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '465'))
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', True)
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', False)
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'mrp@localhost')
MRP_REMINDER_TO = env_list('MRP_REMINDER_TO')
MRP_SITE_URL = os.environ.get('MRP_SITE_URL', 'http://localhost:8001')


# Acumatica (read-only, contract-based REST API)
ACUMATICA = {
    'BASE_URL': os.environ.get('ACUMATICA_BASE_URL', ''),
    'TENANT': os.environ.get('ACUMATICA_TENANT', ''),
    'BRANCH': os.environ.get('ACUMATICA_BRANCH', ''),
    'USERNAME': os.environ.get('ACUMATICA_USERNAME', ''),
    'PASSWORD': os.environ.get('ACUMATICA_PASSWORD', ''),
    'TIMEOUT': int(os.environ.get('ACUMATICA_TIMEOUT', '120')),
    'ENDPOINT': os.environ.get('ACUMATICA_ENDPOINT', 'Default/24.200.001'),
    'MANUFACTURING_ENDPOINT': os.environ.get('ACUMATICA_MANUFACTURING_ENDPOINT', 'MANUFACTURING/24.200.001'),
    'SALES_ORDER_TYPES': env_list('ACUMATICA_SALES_ORDER_TYPES', 'SO'),
}


# Tests must never reach the real Acumatica or mail server, whatever .env contains.
if len(sys.argv) > 1 and sys.argv[1] == 'test':
    ACUMATICA.update({'BASE_URL': '', 'TENANT': '', 'USERNAME': '', 'PASSWORD': ''})
    EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'mrp': {'handlers': ['console'], 'level': os.environ.get('LOG_LEVEL', 'INFO')},
        'acumatica': {'handlers': ['console'], 'level': os.environ.get('LOG_LEVEL', 'INFO')},
    },
}
