"""Phase 6: rows from another application's JSON API. No real network: data-core's opener and
address lookup are replaced."""

import html
import io
import json
import logging
import urllib.error
from decimal import Decimal

import pytest
from data_core import api_fetch

from demo.hooks import SESSION_KEY
from findata.models import Upload

from .conftest import col, define, sql
from .test_db_source import map_and_stage

pytestmark = pytest.mark.django_db

KEY = "Bearer Zq-9-unique-key"
LEDGERS = {
    "status": "ok",
    "data": {
        "ledgers": [
            {"code": "A1", "name": "Cash", "grp": "Assets", "opening": 100.5},
            {"code": "A2", "name": "Bank", "grp": "Assets", "opening": "150000.25"},
        ],
        "shops": [{"id": 9, "city": "Pune"}],
    },
}
CODES = {"code": "account_code", "name": "account_name", "grp": "group_name"}
CODES |= {"opening": "opening_balance"}


class FakeApi:
    def __init__(self, monkeypatch, reply):
        self.reply = reply
        self.requests = []
        monkeypatch.setattr(api_fetch, "_open", self.open)
        monkeypatch.setattr(api_fetch, "_resolve", lambda host: ["10.0.0.5"])

    def open(self, request, limits):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return io.BytesIO(json.dumps(self.reply).encode())


@pytest.fixture(autouse=True)
def upload_folder(settings, tmp_path):
    settings.FINDATA = {**settings.FINDATA, "UPLOAD_DIR": str(tmp_path)}


@pytest.fixture
def api(monkeypatch):
    return FakeApi(monkeypatch, LEDGERS)


def fetch(client, dataset, path="", **form):
    data = {
        "url": "https://erp.example.com/api/ledgers?from=2026-04-01",
        "method": "GET",
        "headers": f"Authorization: {KEY}",
        "rows_at": "data.ledgers",
    }
    return client.post(f"/findata/{dataset.id}/upload/api/{path}", data | form)


def text(response) -> str:
    return html.unescape(response.content.decode())


def test_fetch_map_preview_confirm_and_fetch_again(acme, chart, api):
    response = fetch(acme, chart)
    upload = Upload.objects.latest("id")
    assert response.url == f"/findata/uploads/{upload.id}/map/"
    assert upload.source_kind == Upload.API and upload.rows_at == "data.ledgers"
    assert upload.file_name == "API › erp.example.com/api/ledgers"
    [sent] = api.requests
    assert sent.get_header("Authorization") == KEY
    response = map_and_stage(acme, upload, override=CODES)
    assert response.url == f"/findata/uploads/{upload.id}/", upload.problems
    acme.post(f"/findata/uploads/{upload.id}/confirm/")
    assert sql(
        "SELECT account_code, opening_balance FROM co_acme.chart_of_accounts ORDER BY 1"
    ) == [("A1", Decimal("100.50")), ("A2", Decimal("150000.25"))]
    chart.refresh_from_db()
    assert chart.remembered_source == {
        "kind": "api",
        "url": "https://erp.example.com/api/ledgers?from=2026-04-01",
        "method": "GET",
        "body": "",
        "rows_at": "data.ledgers",
    }
    assert "Fetch again" in acme.get("/findata/").content.decode()
    assert "Fetch again" in acme.get(f"/findata/{chart.id}/").content.decode()
    page = text(acme.get(f"/findata/{chart.id}/upload/?tab=api&again=1"))
    assert "https://erp.example.com/api/ledgers?from=2026-04-01" in page and KEY not in page
    assert "API › erp.example.com/api/ledgers" in text(acme.get(f"/findata/{chart.id}/history/"))

    # Next month: one changed amount, straight to the preview.
    api.reply["data"]["ledgers"][1]["opening"] = 99
    response = fetch(acme, chart, again="1")
    again = Upload.objects.latest("id")
    assert response.url == f"/findata/uploads/{again.id}/"
    again.refresh_from_db()
    assert (again.added, again.changed, again.unchanged) == (0, 1, 1)


def test_post_sends_the_body(acme, chart, api):
    fetch(acme, chart, method="POST", body='{"from": "2026-04-01"}')
    [sent] = api.requests
    assert sent.get_method() == "POST" and json.loads(sent.data) == {"from": "2026-04-01"}
    chart.refresh_from_db()
    assert chart.remembered_source["body"] == '{"from": "2026-04-01"}'


def test_without_rows_at_each_list_is_a_sheet(acme, chart, api):
    fetch(acme, chart, rows_at="")
    upload = Upload.objects.latest("id")
    page = acme.get(f"/findata/uploads/{upload.id}/map/")
    labels = [sheet.label for sheet in page.context["sheets"]]
    assert labels == [
        "API › erp.example.com/api/ledgers › data.ledgers",
        "API › erp.example.com/api/ledgers › data.shops",
    ]


def test_test_button_shows_the_lists(acme, chart, api):
    shown = text(fetch(acme, chart, path="test/", rows_at=""))
    assert "<strong>data.ledgers</strong>: 2 rows · code, name, grp, opening" in shown
    assert "<strong>data.shops</strong>: 1 row" in shown and "Several lists" in shown
    assert Upload.objects.count() == 0


@pytest.mark.parametrize(
    ("form", "reply", "message"),
    [
        ({"rows_at": "data.stores"}, LEDGERS, "no “stores” there"),
        ({}, urllib.error.HTTPError("x", 401, "x", {}, None), "not authorised"),
        ({"url": "erp.example.com"}, LEDGERS, "full web address"),
        ({"headers": "no colon"}, LEDGERS, "Headers, line 1"),
    ],
)
def test_problems_are_explained_and_nothing_is_kept(acme, chart, monkeypatch, form, reply, message):
    FakeApi(monkeypatch, reply)
    response = fetch(acme, chart, **form)
    assert response.status_code == 400 and message in text(response)
    assert Upload.objects.count() == 0
    assert message in text(fetch(acme, chart, path="test/", **form))


def test_headers_are_never_kept(acme, chart, api, caplog):
    caplog.set_level(logging.DEBUG)
    fetch(acme, chart)
    fetch(acme, chart, rows_at="data.stores")  # a failure logs too
    chart.refresh_from_db()
    kept = {f"upload.{k}": v for k, v in Upload.objects.values().first().items()}
    kept |= {"remembered": chart.remembered_source}
    kept |= {f"session.{k}": v for k, v in acme.session.items()}
    assert [name for name, value in kept.items() if "Zq-9" in str(value)] == []
    assert "Zq-9" not in caplog.text


def test_blocked_internal_address(acme, chart, api, settings):
    settings.FINDATA = {**settings.FINDATA, "API_BLOCK_INTERNAL": True}
    assert "inside the network" in text(fetch(acme, chart))
    assert api.requests == []


def test_switched_off(acme, chart, api, settings):
    settings.FINDATA = {**settings.FINDATA, "ALLOW_API_FETCH": False}
    assert ">API</button>" not in acme.get(f"/findata/{chart.id}/upload/").content.decode()
    assert "switched off" in text(fetch(acme, chart))
    assert api.requests == []


def test_another_companys_dataset(client, chart, api):
    session = client.session
    session[SESSION_KEY] = "globex"
    session.save()
    assert fetch(client, chart).status_code == 404
    assert api.requests == []


def test_periodic_report_takes_the_date(acme, api):
    report = define(
        "acme",
        {
            "name": "Balances",
            "kind": "snapshot",
            "description": "",
            "columns": [col("Code", is_key=True), col("Opening", "decimal")],
        },
    )
    fetch(acme, report, as_of="2026-03-31")
    upload = Upload.objects.latest("id")
    assert upload.as_of_date.isoformat() == "2026-03-31"
    map_and_stage(acme, upload, override={"name": "", "grp": ""})
    assert upload.status == Upload.PREVIEW, upload.problems
    acme.post(f"/findata/uploads/{upload.id}/confirm/")
    assert sql("SELECT count(*) FROM co_acme.balances WHERE _as_of_date = '2026-03-31'") == [(2,)]


def test_fetch_again_keeps_the_list_picked_last_time(acme, chart, monkeypatch):
    reply = {"data": {"shops": [{"id": 9}], "ledgers": LEDGERS["data"]["ledgers"]}}
    FakeApi(monkeypatch, reply)
    fetch(acme, chart, rows_at="")
    upload = Upload.objects.latest("id")
    page = acme.get(f"/findata/uploads/{upload.id}/map/")
    ledgers = next(s.label for s in page.context["sheets"] if s.label.endswith("data.ledgers"))
    form = {f"map_{i}": CODES[h] for i, h in enumerate(["code", "name", "grp", "opening"])}
    acme.post(f"/findata/uploads/{upload.id}/map/", form | {"mode": "add_update", "sheet": ledgers})
    acme.post(f"/findata/uploads/{upload.id}/confirm/")
    upload.refresh_from_db()
    assert upload.status == Upload.DONE, upload.problems

    response = fetch(acme, chart, rows_at="", again="1")
    again = Upload.objects.latest("id")
    assert response.url == f"/findata/uploads/{again.id}/"
    again.refresh_from_db()
    assert again.sheet == ledgers and again.unchanged == 2


def test_problems_page_speaks_of_the_reply(acme, chart, monkeypatch):
    reply = {"data": {"ledgers": [{"code": "A1", "name": "Cash", "opening": "lots"}]}}
    FakeApi(monkeypatch, reply)
    fetch(acme, chart)
    upload = Upload.objects.latest("id")
    map_and_stage(acme, upload, override={k: v for k, v in CODES.items() if k != "grp"})
    page = text(acme.get(f"/findata/uploads/{upload.id}/problems/"))
    assert "fetch again" in page and "row 2 is the first record" in page
