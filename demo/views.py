from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .hooks import COMPANIES, SESSION_KEY


@require_POST
def pick_company(request):
    key = request.POST.get("company", "")
    if key in COMPANIES:
        request.session[SESSION_KEY] = key
    else:
        request.session.pop(SESSION_KEY, None)
    return redirect("findata:datasets")
