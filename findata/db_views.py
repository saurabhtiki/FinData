"""Database screens: saved connections (staff), pulling from a server, picking a SQLite table.

The password is only ever in the POST of the request that uses it: never saved, logged or put
in the session (sensitive_post_parameters also hides it from Django's error reports)."""

import logging
from dataclasses import replace

from data_core.databases import check_server
from data_core.loading import UploadRejected
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_datatable.datatable import CHOICE, Column, RowsTable, table_response

from . import conf, db_source, mapping
from . import upload as uploads
from .models import DbConnection, Upload
from .schema_builder import AS_OF
from .upload_views import _failed, _own_upload, _user
from .views import _own_dataset, _page, company_required

logger = logging.getLogger(__name__)


def _own_connection(request, connection_id) -> DbConnection:
    return get_object_or_404(
        DbConnection, pk=connection_id, company_key=request.findata_company.key
    )


def _switched_off(request):
    return _page(
        request,
        "findata/message.html",
        {
            "title": "Database servers are switched off",
            "text": "Reading from a live PostgreSQL or MySQL server is switched off on this site. "
            "The admin can turn it on with ALLOW_DB_CONNECT. SQLite files can still be uploaded.",
        },
        status=403,
    )


# ---------- saved connections (staff) ----------


@require_GET
@company_required(edit=True)
def connections(request):
    if not db_source.servers_allowed():
        return _switched_off(request)
    rows = [
        {
            "id": item.id,
            "name": item.name,
            "kind": item.kind,
            "place": item.place,
            "user": item.user,
            "used_by": ", ".join(db_source.datasets_using(item)),
        }
        for item in DbConnection.objects.filter(company_key=request.findata_company.key)
    ]
    table = RowsTable(
        request.GET,
        rows,
        [
            Column("name", "Name"),
            Column("kind", "Kind", type=CHOICE, choices=dict(DbConnection.KIND_CHOICES)),
            Column("place", "Database"),
            Column("user", "User"),
            Column("used_by", "Used by"),
        ],
        table_id="findata-connections",
        base_url=request.path,
        default_sort=("name",),
        search_fields=("name", "place", "user", "used_by"),
        search_placeholder="Find a connection",
        noun="connections",
    )
    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": request.findata_company,
        "table": table,
    }
    return table_response(
        request,
        table,
        "findata/connections.html",
        "findata/partials/connections_table.html",
        context,
    )


def _connection_from_form(
    request, existing: DbConnection | None, check_name: bool = True
) -> DbConnection:
    """The form's fields → an unsaved DbConnection. Raises UploadRejected. Test connection
    skips the name checks (the edit form sends its own, already saved name)."""
    data = request.POST
    kind = data.get("kind", DbConnection.POSTGRES)
    if kind not in DbConnection.DEFAULT_PORTS:
        raise UploadRejected("Choose PostgreSQL or MySQL.")
    port_text = data.get("port", "").strip() or str(DbConnection.DEFAULT_PORTS[kind])
    if not port_text.isdigit():
        raise UploadRejected("The port must be a number, e.g. 5432 for PostgreSQL, 3306 for MySQL.")
    connection = existing or DbConnection(company_key=request.findata_company.key)
    connection.name = " ".join(data.get("name", "").split())[:100]
    connection.kind = kind
    connection.host = data.get("host", "").strip()[:255]
    connection.port = int(port_text)
    connection.database = data.get("database", "").strip()[:128]
    connection.user = data.get("user", "").strip()[:128]
    connection.require_ssl = data.get("require_ssl") == "on"
    if check_name and not connection.name:
        raise UploadRejected("Give the connection a name, e.g. Tally server.")
    same_name = DbConnection.objects.filter(
        company_key=connection.company_key, name__iexact=connection.name
    ).exclude(pk=connection.pk)
    if check_name and same_name.exists():
        raise UploadRejected(f"There is already a connection called “{connection.name}”.")
    limits = replace(db_source.limits(), allow_servers=True)
    check_server(db_source.server_for(connection, ""), limits)
    return connection


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
@company_required(edit=True)
def connection_edit(request, connection_id: int | None = None):
    if not db_source.servers_allowed():
        return _switched_off(request)
    existing = _own_connection(request, connection_id) if connection_id else None
    connection = existing or DbConnection(company_key=request.findata_company.key)
    error = ""
    if request.method == "POST":
        try:
            connection = _connection_from_form(request, existing)
        except UploadRejected as problem:
            error = str(problem)
            logger.warning("Connection not saved: %s", error)
        else:
            connection.save()
            logger.info("Connection %s saved for %s", connection.name, connection.company_key)
            messages.success(request, f"Saved “{connection.name}”. No password is kept.")
            return redirect("findata:connections")
    context = {
        "company": request.findata_company,
        "connection": connection,
        "kinds": DbConnection.KIND_CHOICES,
        "ports": DbConnection.DEFAULT_PORTS,
        "error": error,
    }
    return _page(request, "findata/connection_form.html", context, status=400 if error else 200)


@sensitive_post_parameters("password")
@require_POST
@company_required(edit=True)
def connection_test(request):
    """htmx: try the form's connection with the typed password; answers in plain words."""
    try:
        if not db_source.servers_allowed():
            raise UploadRejected("Database servers are switched off on this site.")
        connection = _connection_from_form(request, None, check_name=False)
        text = db_source.check_connection(connection, request.POST.get("password", ""))
        ok = True
    except (UploadRejected, uploads.UploadProblems) as problem:
        text = problem.problems[0]["text"] if hasattr(problem, "problems") else str(problem)
        logger.info("Connection test failed: %s", text)
        ok = False
    return render(request, "findata/partials/test_result.html", {"ok": ok, "text": text})


@require_http_methods(["GET", "POST"])
@company_required(edit=True)
def connection_delete(request, connection_id: int):
    connection = _own_connection(request, connection_id)
    if request.method == "POST":
        names = db_source.forget_connection(connection)
        connection.delete()
        logger.info("Connection %s deleted; forgotten by %s", connection.name, names)
        messages.success(request, f"Deleted “{connection.name}”.")
        return redirect("findata:connections")
    context = {
        "company": request.findata_company,
        "connection": connection,
        "used_by": db_source.datasets_using(connection),
    }
    return _page(request, "findata/connection_delete.html", context)


# ---------- pulling from a server ----------


def _password_and_connection(request):
    """The picked saved connection and the typed password. Raises UploadProblems."""
    picked = str(request.POST.get("connection", ""))
    connection = None
    if picked.isdigit():
        connection = DbConnection.objects.filter(
            pk=int(picked), company_key=request.findata_company.key
        ).first()
    if connection is None:
        logger.warning("Pull refused: connection %r is not one of this company's", picked[:20])
        raise uploads.UploadProblems([uploads.problem("Pick one of the saved connections.")])
    return connection, request.POST.get("password", "")


@sensitive_post_parameters("password")
@require_POST
@company_required(upload=True)
def server_tables(request, pk: int):
    """htmx: the tables of the picked connection, as a pick list."""
    _own_dataset(request, pk)
    remembered = request.POST.get("table", "")
    try:
        if not db_source.servers_allowed():
            raise uploads.UploadProblems([uploads.problem("Database servers are switched off.")])
        connection, password = _password_and_connection(request)
        tables = db_source.server_tables(connection, password)
        error = ""
    except uploads.UploadProblems as problem:
        tables, error = [], problem.problems[0]["text"]
        logger.info("Listing tables failed: %s", error)
    context = {"tables": tables, "error": error, "chosen": remembered}
    return render(request, "findata/partials/table_pick.html", context)


@sensitive_post_parameters("password")
@require_POST
@company_required(upload=True)
def pull(request, pk: int):
    dataset = _own_dataset(request, pk)
    if not db_source.servers_allowed():
        return _switched_off(request)
    uploads.sweep_stale()
    try:
        as_of = uploads.picked_date(request.POST.get("as_of")) if dataset.is_snapshot else None
        connection, password = _password_and_connection(request)
        upload = db_source.pull(
            dataset,
            connection,
            password,
            request.POST.get("table", "").strip(),
            request.POST.get("own_query", ""),
            _user(request),
        )
    except uploads.UploadProblems as problem:
        text = problem.problems[0]["text"]
        logger.warning("Pull into %s refused: %s", dataset.name, text)
        messages.error(request, text)
        return redirect(reverse("findata:upload", args=[dataset.id]) + "?tab=database")
    if as_of is not None:
        upload.as_of_date = as_of
        upload.save(update_fields=["as_of_date"])
    if request.POST.get("again") == "1":
        return _straight_to_preview(upload)
    return redirect("findata:upload_map", upload.id)


def _straight_to_preview(upload: Upload):
    """Pull again: the remembered mapping and the last mode, then the preview. Anything
    unclear (a new column, a problem) stops on the mapping or problems page instead."""
    dataset = upload.dataset
    previous = uploads.latest_done(dataset)
    if dataset.is_snapshot:
        mode = Upload.DATES
    elif previous and previous.mode in (Upload.ADD_UPDATE, Upload.FULL_REFRESH):
        mode = previous.mode
    else:
        mode = Upload.ADD_UPDATE
    try:
        with uploads.opened(upload) as sheets:
            sheet = sheets[0]
            chosen = mapping.guess(dataset, sheet.headers)
            new_column = any(h not in dataset.remembered_mapping for h in sheet.headers)
            from_column = AS_OF in chosen.values()
            # A periodic report needs exactly one source for its date: a typed date and a
            # date column both (or neither) → the mapping page, where the user picks.
            unclear_date = dataset.is_snapshot and bool(upload.as_of_date) == from_column
            if new_column or unclear_date or mapping.problems(dataset, chosen):
                upload.mode = mode
                upload.save(update_fields=["mode"])
                return redirect("findata:upload_map", upload.id)
            upload.sheet, upload.mode, upload.mapping = sheet.label, mode, chosen
            uploads.stage(upload, sheet, chosen, mode)
    except uploads.UploadProblems as error:
        upload.save(update_fields=["sheet", "mode", "mapping"])
        return _failed(upload, error.problems)
    return redirect("findata:upload_preview", upload.id)


# ---------- an uploaded SQLite file: pick the table ----------


@require_http_methods(["GET", "POST"])
@company_required(upload=True)
def sqlite_pick(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    if upload.source_kind != Upload.SQLITE or upload.status != Upload.MAPPING:
        return redirect("findata:history", upload.dataset_id)
    dataset = upload.dataset
    remembered = (
        dataset.remembered_source if dataset.remembered_source.get("kind") == "sqlite" else {}
    )
    table = request.POST.get("table", upload.source_table or remembered.get("table", ""))
    own_query = request.POST.get("own_query", remembered.get("select", ""))
    error = ""
    try:
        tables = db_source.sqlite_tables(upload)  # an unreadable file: the problems page
    except uploads.UploadProblems as problem:
        return _failed(upload, problem.problems)
    if request.method == "POST":
        try:
            db_source.choose_sqlite_read(upload, table, own_query)
            return redirect("findata:upload_map", upload.id)
        except uploads.UploadProblems as problem:
            error = problem.problems[0]["text"]
            logger.warning("Upload %s: SQLite pick refused: %s", upload.id, error)
    context = {
        "company": request.findata_company,
        "dataset": dataset,
        "upload": upload,
        "tables": tables,
        "chosen": table,
        "own_query": own_query,
        "error": error,
    }
    return _page(request, "findata/sqlite_pick.html", context, status=400 if error else 200)
