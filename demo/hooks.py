"""Demo host hooks. A real host returns the user's own companies and permissions."""

COMPANIES = {"acme": "ACME Ltd", "globex": "Globex Pvt Ltd"}
SESSION_KEY = "demo_company"


def current_company(request):
    """The company picked at the top of the page, e.g. ("acme", "ACME Ltd"), or None."""
    key = request.session.get(SESSION_KEY)
    return (key, COMPANIES[key]) if key in COMPANIES else None


def list_companies(request):
    return list(COMPANIES.items())


def can_edit_definitions(request):
    return True  # demo: everyone may define datasets
