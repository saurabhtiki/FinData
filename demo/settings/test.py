"""The TEST environment: production-like (debug off) with its own database (findata_test).
Automatic tests (pytest) use demo/settings/pytest.py instead."""

from .base import *

DJANGO_ENV = "test"
DEBUG = False
