"""API tab of the upload page: Test (htmx) and Fetch.

The Headers field may hold a key: it is only in the POST of the request that uses it, never
saved, logged or put in the session (sensitive_post_parameters hides it from error reports)."""

import logging

from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from . import api_source
from . import upload as uploads
from .upload_views import _start_context, _straight_to_preview, _user
from .views import _own_dataset, _page, company_required

logger = logging.getLogger(__name__)

API_FIELDS = ("url", "method", "body", "rows_at")


@sensitive_post_parameters("headers")
@require_POST
@company_required(upload=True)
def api_test(request, pk: int):
    """htmx: fetch once and say what came back, e.g. "data.orders: 120 rows". Nothing is kept."""
    _own_dataset(request, pk)
    try:
        _, groups = api_source.read(*api_source.request_from(request.POST))
        context = {"ok": True, "lists": api_source.summary(groups)}
    except uploads.UploadProblems as problem:
        context = {"ok": False, "text": problem.problems[0]["text"]}
    return render(request, "findata/partials/api_test_result.html", context)


@sensitive_post_parameters("headers")
@require_POST
@company_required(upload=True)
def api_fetch(request, pk: int):
    dataset = _own_dataset(request, pk)
    uploads.sweep_stale()
    try:
        as_of = uploads.picked_date(request.POST.get("as_of")) if dataset.is_snapshot else None
        api_request, rows_at = api_source.request_from(request.POST)
        upload = api_source.fetch(dataset, api_request, rows_at, _user(request))
    except uploads.UploadProblems as problem:
        text = problem.problems[0]["text"]
        logger.warning("API fetch into %s refused: %s", dataset.name, text)
        # The form again with what was typed (except Headers, which are never sent back)
        context = {
            **_start_context(request, dataset),
            "error": text,
            "tab": "api",
            "again": request.POST.get("again") == "1",
            "api": {name: request.POST.get(name, "") for name in API_FIELDS},
            "as_of": request.POST.get("as_of", ""),
        }
        return _page(request, "findata/upload_start.html", context, status=400)
    if as_of is not None:
        upload.as_of_date = as_of
        upload.save(update_fields=["as_of_date"])
    if request.POST.get("again") == "1":
        return _straight_to_preview(upload)
    return redirect("findata:upload_map", upload.id)
