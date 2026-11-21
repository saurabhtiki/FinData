# FinData

Reusable Django add-on: define a per-company financial database (masters, transactions, periodic reports) and keep it in PostgreSQL. Scope: `docs/project.md`.

## Add it to a project
```python
INSTALLED_APPS += ["django.contrib.messages", "django_datatable", "findata"]
FINDATA = {
    "BASE_TEMPLATE": "base.html",                  # htmx 2, Alpine 3, DaisyUI 5, django-datatable
    "GET_COMPANY": "myapp.hooks.current_company",  # request -> ("acme", "ACME Ltd") or None
    "LIST_COMPANIES": "myapp.hooks.my_companies",  # optional, for "Copy from another company"
    "CAN_EDIT_DEFINITIONS": None,                  # optional; default = staff users
    "CAN_UPLOAD": None,                            # optional; default = everyone on the company
    "UPLOAD_DIR": None,                            # optional; files wait here until confirm (default: temp folder)
    # Database servers as a source (SQLite files always work). Off by default.
    "ALLOW_DB_CONNECT": False,                     # True = staff may save PostgreSQL / MySQL servers
    "DB_ALLOWED_HOSTS": [],                        # e.g. ["192.168.1.20"]; [] = any server
    "SQL_MAX_ROWS": 500_000, "SQL_QUERY_SECONDS": 60, "SQL_CONNECT_SECONDS": 10,
    "ALLOW_API_FETCH": True,                       # the API tab: fetch JSON rows (GET / POST)
    "API_BLOCK_INTERNAL": False,                   # True = refuse localhost / 10.x / 192.168.x …
    "API_ALLOWED_HOSTS": [],                       # e.g. ["erp.local"]: allowed even when blocked
    "API_TIMEOUT_SECONDS": 30,                     # reply size limit = DATA_CORE MAX_FILE_MB
}
# urls.py
path("findata/", include("findata.urls")),
```
Reading from MySQL needs `uv add "findata[mysql]"`. Passwords for database servers are never saved: they are typed for each read.

The database must be PostgreSQL (16+). Each company's tables live in schema `co_<company key>`.

## Run the demo
1. Create a PostgreSQL user that can create databases, and a database (see `.env.example`).
2. Copy `.env.example` to `.env.development` and fill in the password.
3. Run:
```
uv sync
uv run python manage.py migrate
uv run python manage.py runserver 8030
```
Open http://localhost:8030/findata/ and pick a company at the top.

## Tests
```
uv run pytest
```
Django creates a throw-away `test_<DB_NAME>` database, so the DB user needs CREATEDB.

## CSS
After changing classes: `.\tools\build_css.ps1`.
