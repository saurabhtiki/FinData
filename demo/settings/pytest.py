"""Settings for the automatic tests only (uv run pytest). Django makes a throw-away database
named test_<DB_NAME>, so the DB user needs the CREATEDB right."""

from .development import *

DEBUG = False
MIDDLEWARE = [m for m in MIDDLEWARE if m != "whitenoise.middleware.WhiteNoiseMiddleware"]
