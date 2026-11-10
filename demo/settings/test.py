"""The TEST environment: production-like (debug off) with its own data/test.db.
Automatic tests (pytest) use demo/settings/pytest.py instead."""

from .base import *
from .base import BASE_DIR, DATABASES

DJANGO_ENV = "test"
DATABASES["default"]["NAME"] = BASE_DIR / "data" / "test.db"
DEBUG = False
