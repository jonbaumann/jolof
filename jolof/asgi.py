"""
ASGI config for jolof project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os
from decouple import config

# Production by default, as in wsgi.py.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', config('DJANGO_SETTINGS_MODULE', default='jolof.settings.production'))

from django.core.asgi import get_asgi_application

application = get_asgi_application()
