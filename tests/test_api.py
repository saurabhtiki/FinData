"""Phase 6: the API (keys, send rows, read rows) and its screens."""

import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from findata import upload as uploads
from findata.api import make_key
from findata.models import ApiKey, Upload

from .conftest import define, sql
from .test_periodic import RECEIVABLES

pytestmark = pytest.mark.django_db

ACCOUNTS = [
    {
        "account_code": "A1",
        "account_name": "Cash",
        "group_name": "Assets",
        "opening_balance": "100.50",
    },
    {"account_code": "A2", "account_name": "Bank", "group_name": "Assets", "opening_balance": 2000},
    {"account_code": "A3", "account_name": "Sales", "group_name": "Income"},
    {"account_code": "A4", "account_name": "Rent", "group_name": "Expenses"},
]


@pytest.fixture(autouse=True)
def upload_folder(settings, tmp_path):
    settings.FINDATA = {**settings.FINDATA, "UPLOAD_DIR": str(tmp_path)}


def new_key(company="acme", send=True, read=True, **extra) -> str:
    _key, raw = make_key(
        company, extra.pop("name", "Tally sync"), can_send=send, can_read=read,
        created_by="asha", expires_on=extra.pop("expires_on", None),
    )  # fmt: skip
    return raw


def send(client, table, body, raw, **extra):
    return client.post(
        f"/findata/api/datasets/{table}/upload/",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
        **extra,
    )


def read(client, table, raw, **params):
    return client.get(
        f"/findata/api/datasets/{table}/rows/", params, HTTP_AUTHORIZATION=f"Bearer {raw}"
    )


def saved_codes(dataset):
    return [
        row[0] for row in sql(f"SELECT account_code FROM co_acme.{dataset.table_name} ORDER BY 1")
    ]


# ---------- keys ----------


def test_a_key_is_stored_only_as_a_hash(db):
    raw = new_key()
    key = ApiKey.objects.get()
    assert raw.startswith("fd_") and len(raw) > 40
    assert key.prefix == raw[:11]
    assert raw not in (key.key_hash, key.name, key.prefix)


@pytest.mark.parametrize(
    "header, status",
    [("", 401), ("Bearer fd_wrong", 401), ("Token abc", 401)],
)
def test_calls_without_a_good_key_are_refused(client, chart, header, status):
    new_key()
    response = client.get(
        "/findata/api/datasets/chart_of_accounts/rows/", HTTP_AUTHORIZATION=header
    )
    assert response.status_code == status
    assert "error" in response.json()


def test_switched_off_expired_and_missing_rights_are_refused(client, chart):
    raw = new_key(read=False)
    assert read(client, "chart_of_accounts", raw).status_code == 403
    only_read = new_key(send=False, name="Power BI")
    assert send(client, "chart_of_accounts", {"rows": ACCOUNTS}, only_read).status_code == 403
    ended = new_key(name="Old", expires_on=timezone.localdate() - timedelta(days=1))
    assert "stopped working" in read(client, "chart_of_accounts", ended).json()["error"]
    ApiKey.objects.filter(name="Power BI").update(active=False)
    assert "switched off" in read(client, "chart_of_accounts", only_read).json()["error"]


def test_last_used_is_kept(client, chart):
    raw = new_key()
    assert ApiKey.objects.get().last_used_at is None
    read(client, "chart_of_accounts", raw)
    assert ApiKey.objects.get().last_used_at is not None


def test_https_is_required_and_the_switch_turns_it_off(client, chart, settings):
    raw = new_key()
    settings.FINDATA = {**settings.FINDATA, "API_REQUIRE_HTTPS": True}
    assert "https" in read(client, "chart_of_accounts", raw).json()["error"]
    secure = client.get(
        "/findata/api/datasets/chart_of_accounts/rows/",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
        secure=True,
    )
    assert secure.status_code == 200
    settings.FINDATA = {**settings.FINDATA, "ALLOW_API": False}
    assert read(client, "chart_of_accounts", raw).status_code == 404


def test_a_key_sees_only_its_own_company(client, chart):
    raw = new_key(company="globex")
    response = read(client, "chart_of_accounts", raw)
    assert response.status_code == 404
    assert "no dataset" in response.json()["error"]


# ---------- send ----------


def test_send_adds_then_updates_and_shows_in_history(client, acme, chart):
    raw = new_key()
    first = send(client, "chart_of_accounts", {"rows": ACCOUNTS[:2]}, raw)
    assert first.status_code == 201, first.json()
    body = first.json()
    assert (body["added"], body["changed"], body["deleted"], body["saved"]) == (2, 0, 0, True)
    assert body["changes_url"].endswith(f"/findata/{chart.id}/changes/?upload={body['upload']}")
    upload = Upload.objects.get(pk=body["upload"])
    assert (upload.status, upload.user, upload.source_kind) == (
        Upload.DONE, "API: Tally sync", Upload.API,
    )  # fmt: skip
    assert sql("SELECT opening_balance FROM co_acme.chart_of_accounts ORDER BY 1") == [
        (pytest.approx(100.50),), (2000,),
    ]  # fmt: skip
    changed = [{"account_code": "A1", "account_name": "Cash in hand"}]
    second = send(client, "chart_of_accounts", {"mode": "update", "rows": changed}, raw)
    assert (second.json()["added"], second.json()["changed"]) == (0, 1)
    history = acme.get(f"/findata/{chart.id}/history/").content.decode()
    assert "API: Tally sync" in history
    undo = Upload.objects.get(pk=second.json()["upload"])
    acme.post(f"/findata/uploads/{undo.id}/undo/")
    assert sql("SELECT account_name FROM co_acme.chart_of_accounts WHERE account_code = 'A1'") == [
        ("Cash",)
    ]


def test_dry_run_saves_nothing(client, chart):
    raw = new_key()
    response = send(client, "chart_of_accounts", {"rows": ACCOUNTS, "dry_run": True}, raw)
    assert response.status_code == 200
    assert response.json() == {
        "dry_run": True, "saved": False, "added": 4, "changed": 0, "deleted": 0, "unchanged": 0,
    }  # fmt: skip
    assert saved_codes(chart) == []
    assert not Upload.objects.exists()  # a call that saves nothing leaves nothing


def test_problems_come_back_with_row_and_nothing_is_saved(client, chart):
    raw = new_key()
    rows = [dict(ACCOUNTS[0], opening_balance="abc"), {"account_code": "A9"}]
    response = send(client, "chart_of_accounts", {"rows": rows}, raw)
    assert response.status_code == 400
    body = response.json()
    assert body["problem_count"] == 2
    assert body["problems"][0]["row"] == 1 and "isn't a number" in body["problems"][0]["problem"]
    assert body["problems"][1] == {
        "row": 2, "key": "A9", "problem": "Account name is blank, but it's required.",
    }  # fmt: skip
    assert saved_codes(chart) == []


@pytest.mark.parametrize(
    "body, text",
    [
        ({"rows": []}, "empty"),
        ({"rows": "x"}, "as a list"),
        ({"rows": [{"nope": 1}]}, "isn't a column"),
        ({"rows": [{"account_code": ["A1"]}]}, "single value"),
        ({"mode": "replace", "rows": ACCOUNTS}, "“mode”"),
    ],
)
def test_bad_bodies_are_explained(client, chart, body, text):
    response = send(client, "chart_of_accounts", body, new_key())
    assert response.status_code == 400
    assert text in response.json()["error"]


def test_not_json_is_explained(client, chart):
    response = client.post(
        "/findata/api/datasets/chart_of_accounts/upload/",
        data="rows=1",
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {new_key()}",
    )
    assert response.status_code == 400 and "isn't valid JSON" in response.json()["error"]


def test_a_missing_link_is_a_problem(client, sales):
    raw = new_key()
    row = {"voucher_no": "1023", "line_no": 1, "voucher_date": "2026-04-02",
           "account_code": "A9", "amount": "550"}  # fmt: skip
    response = send(client, "sales", {"rows": [row]}, raw)
    assert response.status_code == 400
    assert "A9" in response.json()["problems"][0]["problem"]


def test_full_refresh_deleting_more_than_half_needs_the_flag(client, chart):
    raw = new_key()
    send(client, "chart_of_accounts", {"rows": ACCOUNTS}, raw)
    body = {"mode": "full", "rows": ACCOUNTS[:1]}
    refused = send(client, "chart_of_accounts", body, raw)
    assert refused.status_code == 400 and "allow_big_delete" in refused.json()["error"]
    assert len(saved_codes(chart)) == 4
    done = send(client, "chart_of_accounts", {**body, "allow_big_delete": True}, raw)
    assert done.status_code == 201 and done.json()["deleted"] == 3
    assert saved_codes(chart) == ["A1"]


def test_row_limit(client, chart, settings):
    settings.FINDATA = {**settings.FINDATA, "API_MAX_ROWS": 2}
    response = send(client, "chart_of_accounts", {"rows": ACCOUNTS}, new_key())
    assert response.status_code == 413 and "Send a file" in response.json()["error"]


def test_a_busy_dataset_answers_409(client, chart, monkeypatch):
    def busy(*args, **kwargs):
        raise uploads.UploadBusy([uploads.problem("Another upload of Chart is in progress.")])

    monkeypatch.setattr(uploads, "stage", busy)
    response = send(client, "chart_of_accounts", {"rows": ACCOUNTS}, new_key())
    assert response.status_code == 409 and "in progress" in response.json()["error"]
    assert not Upload.objects.exists()


def test_an_unexpected_error_answers_json_and_leaves_nothing(client, chart, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(uploads, "stage", broken)
    response = send(client, "chart_of_accounts", {"rows": ACCOUNTS}, new_key())
    assert response.status_code == 500 and "Nothing was saved" in response.json()["error"]
    assert not Upload.objects.exists()


def test_nan_is_refused(client, chart):
    response = client.post(
        "/findata/api/datasets/chart_of_accounts/upload/",
        data='{"rows": [{"account_code": "A1", "account_name": "Cash", "opening_balance": NaN}]}',
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {new_key()}",
    )
    assert response.status_code == 400 and "NaN" in response.json()["error"]


def test_a_body_over_django_s_memory_limit_is_read(client, chart, settings):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 1_000
    rows = [{"account_code": f"C{n}", "account_name": f"Party {n}"} for n in range(200)]
    response = send(client, "chart_of_accounts", {"rows": rows}, new_key())
    assert response.status_code == 201 and response.json()["added"] == 200


def test_a_file_is_mapped_by_name(client, chart):
    csv = "Account Code,Account Name,Group\nA1,Cash,Assets\nA2,Bank,Assets\n"
    response = client.post(
        "/findata/api/datasets/chart_of_accounts/upload/",
        {"file": SimpleUploadedFile("ledgers.csv", csv.encode()), "mode": "full"},
        HTTP_AUTHORIZATION=f"Bearer {new_key()}",
    )
    assert response.status_code == 201, response.json()
    assert saved_codes(chart) == ["A1", "A2"]
    assert Upload.objects.get().file_name == "ledgers.csv"


def test_a_file_is_not_mapped_by_a_similar_name(client, chart):
    csv = "Account Code,Account Nme\nA1,Cash\n"  # a person would check "Nme" on the screen
    response = client.post(
        "/findata/api/datasets/chart_of_accounts/upload/",
        {"file": SimpleUploadedFile("x.csv", csv.encode())},
        HTTP_AUTHORIZATION=f"Bearer {new_key()}",
    )
    assert response.status_code == 400
    assert "Account name is required" in response.json()["problems"][0]["problem"]


def test_a_file_without_a_required_column_is_refused(client, chart):
    response = client.post(
        "/findata/api/datasets/chart_of_accounts/upload/",
        {"file": SimpleUploadedFile("x.csv", b"Account Code\nA1\n")},
        HTTP_AUTHORIZATION=f"Bearer {new_key()}",
    )
    assert response.status_code == 400
    assert "Account name is required" in response.json()["problems"][0]["problem"]


def test_periodic_report_needs_an_as_of_date(client, db):
    define("acme", RECEIVABLES)
    raw = new_key()
    rows = [{"party": "P1", "amount": "10"}]
    missing = send(client, "receivables", {"rows": rows}, raw)
    assert missing.status_code == 400 and "as_of" in missing.json()["error"]
    done = send(client, "receivables", {"as_of": "2026-03-31", "rows": rows}, raw)
    assert done.status_code == 201
    assert done.json()["dates"][0]["date"] == "2026-03-31"
    per_row = send(client, "receivables", {"rows": [dict(rows[0], _as_of_date="2026-02-28")]}, raw)
    assert per_row.status_code == 201
    latest = read(client, "receivables", raw).json()
    assert latest["as_of"] == "2026-03-31" and latest["rows"][0]["_as_of_date"] == "2026-03-31"
    older = read(client, "receivables", raw, as_of="2026-02-28").json()
    assert older["count"] == 1 and older["as_of"] == "2026-02-28"


# ---------- read ----------


def test_read_pages_filters_and_types(client, chart, settings):
    raw = new_key()
    send(client, "chart_of_accounts", {"rows": ACCOUNTS}, raw)
    settings.FINDATA = {**settings.FINDATA, "API_PAGE_ROWS": 3}
    first = read(client, "chart_of_accounts", raw).json()
    assert (first["count"], first["pages"], first["page"]) == (4, 2, 1)
    assert [row["account_code"] for row in first["rows"]] == ["A1", "A2", "A3"]
    assert first["rows"][0]["opening_balance"] == "100.50"
    assert first["rows"][2]["opening_balance"] is None
    assert "page=2" in first["next"]
    second = client.get(first["next"], HTTP_AUTHORIZATION=f"Bearer {raw}").json()
    assert [row["account_code"] for row in second["rows"]] == ["A4"] and second["next"] is None
    assert read(client, "chart_of_accounts", raw, group_name="assets").json()["count"] == 2
    assert read(client, "chart_of_accounts", raw, account_name__contains="a").json()["count"] == 3
    between = read(client, "chart_of_accounts", raw, opening_balance__from="200")
    assert [row["account_code"] for row in between.json()["rows"]] == ["A2"]


@pytest.mark.parametrize(
    "params, text",
    [
        ({"nope": "1"}, "isn't a filter"),
        ({"opening_balance__from": "abc"}, "isn't a number"),
        ({"group_name__from": "a"}, "numbers and dates only"),
        ({"page": "x"}, "whole number"),
    ],
)
def test_bad_filters_are_explained(client, chart, params, text):
    response = read(client, "chart_of_accounts", new_key(), **params)
    assert response.status_code == 400 and text in response.json()["error"]


# ---------- screens ----------


def test_making_a_key_shows_it_once(acme, db):
    response = acme.post("/findata/api/keys/new/", {"name": "Tally sync", "can_send": "on"})
    assert response.status_code == 200
    key = ApiKey.objects.get()
    assert (key.name, key.can_send, key.can_read, key.created_by) == ("Tally sync", True, False, "")
    page = response.content.decode()
    shown = page.split('x-ref="key">')[1].split("<")[0]
    assert shown.startswith(key.prefix)
    assert shown not in acme.get("/findata/api/keys/").content.decode()


@pytest.mark.parametrize(
    "form, text",
    [
        ({"name": "", "can_send": "on"}, "Give the key a name"),
        ({"name": "X"}, "Tick what the key may do"),
        ({"name": "X", "can_read": "on", "expires_on": "2020-01-01"}, "in the past"),
    ],
)
def test_new_key_form_problems(acme, db, form, text):
    response = acme.post("/findata/api/keys/new/", form)
    assert response.status_code == 400 and text in response.content.decode()
    assert not ApiKey.objects.exists()


def test_switch_off_and_who_may_manage_keys(acme, client, chart, settings):
    raw = new_key()
    key = ApiKey.objects.get()
    acme.post(f"/findata/api/keys/{key.id}/off/")
    assert read(client, "chart_of_accounts", raw).status_code == 401
    settings.FINDATA = {**settings.FINDATA, "CAN_EDIT_DEFINITIONS": lambda request: False}
    assert acme.get("/findata/api/keys/").status_code == 403
    help_page = acme.get("/findata/api/help/")
    assert help_page.status_code == 200  # anyone may read the help
    assert b"/findata/api/keys/" not in help_page.content
    datasets = acme.get("/findata/").content.decode()
    assert "API help" in datasets and "/findata/api/keys/" not in datasets


def test_help_lists_columns_and_examples(acme, sales, chart):
    page = acme.get(f"/findata/api/help/?dataset={sales.id}").content.decode()
    for text in ("voucher_no", "Links to Chart of Accounts", "/findata/api/datasets/sales/upload/"):
        assert text in page


def test_api_switched_off_hides_the_screens(acme, db, settings):
    settings.FINDATA = {**settings.FINDATA, "ALLOW_API": False}
    assert acme.get("/findata/api/keys/").status_code == 403
    assert "API keys" not in acme.get("/findata/").content.decode()


def test_rows_read_can_be_sent_back_unchanged(client, db):
    from .conftest import col

    define("acme", {
        "name": "Bank lines", "kind": "transaction", "description": "",
        "columns": [col("Line", "integer", is_key=True), col("At", "datetime"),
                    col("Cleared", "boolean"), col("Amount", "decimal")],
    })  # fmt: skip
    raw = new_key()
    rows = [{"line": 1, "at": "31-03-2026 14:30", "cleared": "Yes", "amount": "10.5"}]
    assert send(client, "bank_lines", {"rows": rows}, raw).status_code == 201
    got = read(client, "bank_lines", raw).json()["rows"]
    assert got == [{"line": 1, "at": "2026-03-31 14:30:00", "cleared": True, "amount": "10.50"}]
    again = send(client, "bank_lines", {"rows": got}, raw).json()
    assert (again["added"], again["changed"], again["unchanged"]) == (0, 0, 1)
