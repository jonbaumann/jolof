"""
Django settings for jolof project.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.2/ref/settings/
"""

from pathlib import Path
from decouple import config

# The app version, shown in the footer and the What's New popup. Bump it with
# each release and add a matching section to CHANGELOG.md.
VERSION = '1.33.0'

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')

# DEBUG and ALLOWED_HOSTS are set in development.py and production.py.

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'main.apps.MainConfig',
    'crispy_forms',
    'crispy_bootstrap5',
    'django_htmx',
    'rest_framework',
    'drf_spectacular',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Both need request.user, so both sit after AuthenticationMiddleware.
    'main.audit.AuditActorMiddleware',
    'main.middleware.TimezoneMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    # After HtmxMiddleware, so it can tell an htmx request from a page load.
    'main.middleware.ForcePasswordChangeMiddleware',
]

ROOT_URLCONF = 'jolof.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'main.context_processors.app_version',
                'main.context_processors.whats_new',
                'main.context_processors.permissions',
                'main.context_processors.theme',
            ],
        },
    },
]

WSGI_APPLICATION = 'jolof.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME'),
        'USER': config('DB_USER'),
        'PASSWORD': config('DB_PASSWORD'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

AWS_ACCESS_KEY_ID = config('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = config('AWS_SECRET_ACCESS_KEY')
AWS_S3_REGION_NAME = config('AWS_S3_REGION_NAME')

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "bucket_name": config('AWS_MEDIA_BUCKET_NAME'),
            "region_name": AWS_S3_REGION_NAME,
            "access_key": AWS_ACCESS_KEY_ID,
            "secret_key": AWS_SECRET_ACCESS_KEY,
            "querystring_auth": True,
            "signature_version": "s3v4",
            "endpoint_url": f"https://s3.{AWS_S3_REGION_NAME}.amazonaws.com",
        },
    },
    "staticfiles": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "bucket_name": config('AWS_STATIC_BUCKET_NAME'),
            "region_name": AWS_S3_REGION_NAME,
            "access_key": AWS_ACCESS_KEY_ID,
            "secret_key": AWS_SECRET_ACCESS_KEY,
            "querystring_auth": False,
            "endpoint_url": f"https://s3.{AWS_S3_REGION_NAME}.amazonaws.com",
        },
    },
}
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST API (main/api/). Session tokens are the only way in: no browser cookies,
# so there's no CSRF to worry about. JSON only, no browsable API -- the
# Swagger page at /api/v1/docs/ is the place to explore it.
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['main.api.authentication.SessionTokenAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['main.api.permissions.RolePermission'],
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser'],
    'DEFAULT_PAGINATION_CLASS': 'main.api.pagination.Pagination',
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'main.api.exceptions.exception_handler',
    'DEFAULT_THROTTLE_RATES': {'api-login': '10/min'},
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Jolof API',
    'DESCRIPTION': 'Read recipes, tags and the meal planner. Log in at auth/login/ '
                   'for a session token, then send it as an "Authorization: Bearer" header. '
                   'The API Reference page in Jolof (user menu) explains each endpoint with examples.',
    'VERSION': '1',
    'SERVE_INCLUDE_SCHEMA': False,
    # The docs pages themselves are for people already logged in to Jolof.
    'SERVE_AUTHENTICATION': ['rest_framework.authentication.SessionAuthentication'],
    'SERVE_PERMISSIONS': ['main.api.permissions.HasAnyApiPermission'],
    'SWAGGER_UI_SETTINGS': {'persistAuthorization': True},
}

LOGIN_URL = '/login'
LOGIN_REDIRECT_URL = '/home'
LOGOUT_REDIRECT_URL = '/login'
