"""Settings for the automatic tests only (uv run pytest)."""

from .test import *

MIDDLEWARE = [m for m in MIDDLEWARE if m != "whitenoise.middleware.WhiteNoiseMiddleware"]
