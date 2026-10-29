"""
WSGI config for jolof project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os
from decouple import config

# Production by default: this is what an app server loads, and a missing
# variable mustn't quietly turn DEBUG on.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', config('DJANGO_SETTINGS_MODULE', default='jolof.settings.production'))

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
