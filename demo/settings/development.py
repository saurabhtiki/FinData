from .base import *

DEBUG = True
WHITENOISE_AUTOREFRESH = True

# Demo: reading from database servers is on, for this machine only.
FINDATA = {**FINDATA, "ALLOW_DB_CONNECT": True, "DB_ALLOWED_HOSTS": ["localhost", "127.0.0.1"]}
