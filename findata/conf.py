"""FinData settings. A host overrides any of them with one block in settings.py:

FINDATA = {
    "BASE_TEMPLATE": "base.html",
    "GET_COMPANY": "myapp.hooks.current_company",    # request -> ("acme", "ACME Ltd") or None
    "LIST_COMPANIES": "myapp.hooks.my_companies",    # request -> [("acme", "ACME Ltd"), ...]
    "CAN_EDIT_DEFINITIONS": "myapp.hooks.can_edit",  # request -> bool
    "CAN_UPLOAD": "myapp.hooks.can_upload",          # request -> bool
    "ALLOW_DB_CONNECT": True,
    "DB_ALLOWED_HOSTS": ["192.168.1.20"],
    "ALLOW_API": True,
}
"""

from django.conf import settings

DEFAULTS = {
    # Template the pages extend. It must have `title`, `extra_head` and `content` blocks and
    # load htmx 2, Alpine 3, DaisyUI 5 and django-datatable's CSS / JS.
    "BASE_TEMPLATE": "findata/base.html",
    # Required: which company the user is working on. The host owns companies and users.
    "GET_COMPANY": None,
    # Optional: companies the user may copy definitions from. None hides "Copy".
    "LIST_COMPANIES": None,
    # Optional: who may create / change / delete definitions. None = Django staff users.
    "CAN_EDIT_DEFINITIONS": None,
    # Optional: who may upload and undo. None = everyone who can work on the company.
    "CAN_UPLOAD": None,
    # Folder for uploaded files between "upload" and "confirm". None = the system temp folder.
    "UPLOAD_DIR": None,
    # Live database servers (PostgreSQL / MySQL) as a source. Off until the admin turns it on;
    # SQLite files can always be uploaded. MySQL needs: uv add "findata[mysql]".
    "ALLOW_DB_CONNECT": False,
    # Only these servers may be read, e.g. ["192.168.1.20", "erp.local"]. [] = any server.
    "DB_ALLOWED_HOSTS": [],
    "SQL_MAX_ROWS": 500_000,  # one pull reads at most this many rows
    "SQL_QUERY_SECONDS": 60,  # a query running longer is stopped
    "SQL_CONNECT_SECONDS": 10,  # wait this long for the server to answer
    # The API (send / read rows with a key). Off until the admin turns it on.
    "ALLOW_API": False,
    # Refuse API calls over plain http://, so a key can't be read on the way.
    "API_REQUIRE_HTTPS": True,
    "API_MAX_ROWS": 200_000,  # rows in one JSON call (a file uses the upload size limit)
    "API_PAGE_ROWS": 1_000,  # rows per page when reading
}


def get(name: str):
    """One setting, from the host's FINDATA block or the default."""
    return getattr(settings, "FINDATA", {}).get(name, DEFAULTS[name])
