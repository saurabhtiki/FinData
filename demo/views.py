from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST

from .hooks import COMPANIES, SESSION_KEY


@require_POST
def pick_company(request):
    key = request.POST.get("company", "")
    if key in COMPANIES:
        request.session[SESSION_KEY] = key
    else:
        request.session.pop(SESSION_KEY, None)
    return redirect("findata:datasets")


@require_GET
def sample_ledgers(request):
    """A pretend ERP API for trying the API tab: GET /demo/api/ledgers/ (Rows at: data.ledgers)."""
    if request.headers.get("Authorization") != "Bearer demo-key":
        return JsonResponse({"error": "Send the header Authorization: Bearer demo-key"}, status=401)
    ledgers = [
        {"code": "1001", "name": "Cash", "group": {"name": "Current assets"}, "opening": "2500.00"},
        {
            "code": "1002",
            "name": "HDFC Bank",
            "group": {"name": "Current assets"},
            "opening": 150000.5,
        },
        {
            "code": "2001",
            "name": "Sundry creditors",
            "group": {"name": "Liabilities"},
            "opening": 0,
        },
    ]
    return JsonResponse({"status": "ok", "data": {"ledgers": ledgers, "count": len(ledgers)}})
