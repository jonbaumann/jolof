"""
Django settings specific to PRODUCTION environment.

TLS, HTTPS redirects, HSTS and secure cookies are left to whatever terminates
TLS in front of the app; see the README.
"""
from decouple import Csv, config

from .base import *

DEBUG = False

# Required: the hostnames the site is served on, comma-separated
# (e.g. "recipes.example.com,jolof"). No default, so a missing value fails at
# startup rather than turning every request into a 400.
ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())
