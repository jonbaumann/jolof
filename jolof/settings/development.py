"""
Django settings specific to DEVELOPMENT environment.

"""
from decouple import Csv, config

from .base import *

DEBUG = True

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())
