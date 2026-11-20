"""Demo-only template values."""

from django.conf import settings

CSS_FILE = settings.BASE_DIR / "static" / "css" / "tailwind.css"


def css_version(request):
    """{{ css_version }}: changes when tailwind.css is rebuilt, so browsers fetch the new file
    instead of an old cached one (e.g. tailwind.css?v=1762921231)."""
    try:
        return {"css_version": int(CSS_FILE.stat().st_mtime)}
    except OSError:
        return {"css_version": 0}


def companies(request):
    """The company picker in the demo's top bar."""
    from .hooks import COMPANIES, SESSION_KEY

    return {"demo_companies": COMPANIES, "demo_company": request.session.get(SESSION_KEY)}
