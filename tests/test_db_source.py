"""Phase 2b: reading a dataset from a SQLite file or a live database server."""

import html
import logging
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.conf import settings as django_settings
from django.core.files.uploadedfile import SimpleUploadedFile

from findata import db_source
from findata.models import Dataset, DbConnection, Upload

from .conftest import sql

pytestmark = pytest.mark.django_db

PASSWORD = django_settings.DATABASES["default"]["PASSWORD"]
LEDGERS = (
    "SELECT * FROM (VALUES ('A1', 'Cash', 'Assets', 100.50::numeric(18,2)), "
    "('a2', 'Bank', 'Assets', 150000.25)) AS v(account_code, account_name, grp, opening)"
)


@pytest.fixture(autouse=True)
def upload_folder(settings, tmp_path):
    settings.FINDATA = {
        **settings.FINDATA,
        "UPLOAD_DIR": str(tmp_path),
        "ALLOW_DB_CONNECT": True,
        "DB_ALLOWED_HOSTS": [],
    }


@pytest.fixture
def server(db):
    """A saved connection to this test database itself (the source is an own query)."""
    database = django_settings.DATABASES["default"]
    return DbConnection.objects.create(
        company_key="acme",
        name="Local server",
        host=database["HOST"] or "localhost",
        port=int(database["PORT"] or 5432),
        database=database["NAME"],  # already test_… while the tests run
        user=database["USER"],
    )


def sqlite_file(tmp_path, rows, name="shop.db") -> bytes:
    path = tmp_path / f"src_{name}"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ledgers (code TEXT, name TEXT, grp TEXT, opening REAL)")
    connection.executemany("INSERT INTO ledgers VALUES (?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path.read_bytes()


def map_and_stage(client, upload, mode="add_update", override=None):
    page = client.get(f"/findata/uploads/{upload.id}/map/")
    headers = page.context["headers_json"]
    chosen = dict(zip(headers, page.context["chosen_json"], strict=True)) | (override or {})
    form = {f"map_{index}": chosen[header] for index, header in enumerate(headers)}
    form.update(mode=mode, sheet=page.context["sheet"].label)
    response = client.post(f"/findata/uploads/{upload.id}/map/", form)
    upload.refresh_from_db()
    return response


CODES = {"code": "account_code", "name": "account_name", "grp": "group_name"}
CODES |= {"opening": "opening_balance"}


def page_text(response) -> str:
    return html.unescape(response.content.decode())


def pull(client, dataset, server, password=PASSWORD, follow=False, **form):
    data = {"connection": server.id, "password": password, "table": "", "own_query": LEDGERS}
    return client.post(f"/findata/{dataset.id}/upload/database/", data | form, follow=follow)


# ---------- SQLite file ----------


def test_sqlite_file_table_to_saved_rows(acme, chart, tmp_path):
    data = sqlite_file(tmp_path, [("A1", "Cash", "Assets", 100.5), ("A2", "Bank", None, 2.25)])
    response = acme.post(
        f"/findata/{chart.id}/upload/", {"file": SimpleUploadedFile("shop.db", data)}
    )
    upload = Upload.objects.latest("id")
    assert upload.source_kind == Upload.SQLITE
    assert response.url == f"/findata/uploads/{upload.id}/table/"
    page = acme.get(response.url)
    assert [table.name for table in page.context["tables"]] == ["ledgers"]
    response = acme.post(response.url, {"table": "ledgers"})
    assert response.url == f"/findata/uploads/{upload.id}/map/"
    response = map_and_stage(acme, upload, override=CODES)
    assert response.url == f"/findata/uploads/{upload.id}/", upload.problems
    assert (upload.added, upload.changed) == (2, 0)
    acme.post(f"/findata/uploads/{upload.id}/confirm/")
    assert sql(
        "SELECT account_code, opening_balance FROM co_acme.chart_of_accounts ORDER BY 1"
    ) == [
        ("A1", Decimal("100.50")),
        ("A2", Decimal("2.25")),
    ]
    chart.refresh_from_db()
    assert chart.remembered_source["kind"] == "sqlite"
    assert chart.remembered_source["table"] == "ledgers"
    assert "shop.db · ledgers" in acme.get(f"/findata/{chart.id}/history/").content.decode()


def test_sqlite_own_query_must_only_read(acme, chart, tmp_path):
    data = sqlite_file(tmp_path, [("A1", "Cash", "Assets", 1)])
    response = acme.post(
        f"/findata/{chart.id}/upload/", {"file": SimpleUploadedFile("shop.db", data)}
    )
    response = acme.post(response.url, {"table": "", "own_query": "DELETE FROM ledgers"})
    assert response.status_code == 400
    assert "Only a SELECT" in response.context["error"]


def test_a_fake_db_file_is_explained(acme, chart):
    response = acme.post(
        f"/findata/{chart.id}/upload/", {"file": SimpleUploadedFile("shop.db", b"code\nA1\n")}
    )
    upload = Upload.objects.latest("id")
    response = acme.get(response.url, follow=True)
    upload.refresh_from_db()
    assert "not a SQLite database" in upload.problems[0]["text"]


def test_problems_name_the_result_row_and_key(acme, chart, tmp_path):
    data = sqlite_file(tmp_path, [("A1", "Cash", "x", 1), ("A2", "Bank", "x", "lots")])
    response = acme.post(
        f"/findata/{chart.id}/upload/", {"file": SimpleUploadedFile("shop.db", data)}
    )
    upload = Upload.objects.latest("id")
    acme.post(response.url, {"table": "ledgers"})
    map_and_stage(acme, upload, override=CODES)
    assert upload.problems == [
        {
            "row": 2,
            "key": "A2",
            "text": "Opening balance: “lots” isn't a number (use e.g. 1500.50 or 1,500.50).",
        }
    ]
    page = acme.get(f"/findata/uploads/{upload.id}/problems/").content.decode()
    assert "Result row" in page


# ---------- a live server ----------


def test_pull_from_server_preview_and_pull_again(acme, chart, server):
    response = pull(acme, chart, server)
    upload = Upload.objects.latest("id")
    assert response.url == f"/findata/uploads/{upload.id}/map/", response
    assert (upload.source_kind, upload.file_name) == (Upload.SERVER, "Local server")
    response = map_and_stage(
        acme, upload, override={"grp": "group_name", "opening": "opening_balance"}
    )
    assert response.url == f"/findata/uploads/{upload.id}/", upload.problems
    acme.post(f"/findata/uploads/{upload.id}/confirm/")
    assert sql(
        "SELECT account_code, opening_balance FROM co_acme.chart_of_accounts ORDER BY 1"
    ) == [
        ("A1", Decimal("100.50")),
        ("a2", Decimal("150000.25")),
    ]
    chart.refresh_from_db()
    assert chart.remembered_source == {
        "kind": "server",
        "connection": server.id,
        "table": "",
        "select": LEDGERS,
    }
    assert "Pull again" in acme.get("/findata/").content.decode()

    # Next month: one changed amount, straight to the preview (keys ignore capitals: A2 = a2).
    changed = LEDGERS.replace("150000.25", "99.00").replace("'a2'", "'A2'")
    response = pull(acme, chart, server, own_query=changed, again="1")
    again = Upload.objects.latest("id")
    assert response.url == f"/findata/uploads/{again.id}/"
    again.refresh_from_db()
    assert (again.added, again.changed, again.unchanged) == (0, 1, 1)


def test_periodic_report_pull_again_takes_the_date(acme, server):
    from .conftest import col, define

    report = define(
        "acme",
        {
            "name": "Balances",
            "kind": "snapshot",
            "description": "",
            "columns": [col("Code", is_key=True), col("Opening", "decimal")],
        },
    )
    response = pull(acme, report, server, as_of="2026-03-31")
    upload = Upload.objects.latest("id")
    assert upload.as_of_date.isoformat() == "2026-03-31"
    override = {"account_code": "code", "account_name": "", "grp": "", "opening": "opening"}
    map_and_stage(acme, upload, override=override)
    assert upload.status == Upload.PREVIEW, upload.problems
    acme.post(f"/findata/uploads/{upload.id}/confirm/")

    # Pull again for 30-04: straight to the preview, the new date only.
    response = pull(acme, report, server, again="1", as_of="2026-04-30")
    again = Upload.objects.latest("id")
    assert response.url == f"/findata/uploads/{again.id}/", again.problems
    again.refresh_from_db()
    assert again.dates[0]["date"] == "2026-04-30" and again.added == 2
    # Pull again with no date and no date column: the mapping page asks for it.
    response = pull(acme, report, server, again="1", as_of="")
    assert response.url.endswith("/map/")


def test_wrong_password_leaves_nothing(acme, chart, server):
    before = Upload.objects.count()
    response = pull(acme, chart, server, password="not-the-password", follow=True)
    assert Upload.objects.count() == before
    shown = page_text(response)
    assert "The user name or password is wrong." in shown


def test_password_is_never_kept(acme, chart, server, caplog):
    """A made-up password (it fails, so the error paths log too) is found nowhere; the real
    one is used by a good pull too, so its success path runs as well."""
    caplog.set_level(logging.DEBUG)
    secret = "Zq-9!unique-pw"
    pull(acme, chart, server, password=secret, follow=True)
    pull(acme, chart, server)
    upload = Upload.objects.latest("id")
    kept = Upload.objects.filter(pk=upload.pk).values().first()
    kept |= {f"connection.{k}": v for k, v in DbConnection.objects.values().first().items()}
    kept |= {f"session.{k}": v for k, v in acme.session.items()}
    assert [name for name, value in kept.items() if secret in str(value)] == []
    assert secret not in caplog.text


def test_query_that_writes_is_refused(acme, chart, server):
    response = pull(acme, chart, server, own_query="DELETE FROM findata_upload", follow=True)
    shown = page_text(response)
    assert "Only a SELECT query can run here" in shown


def test_row_limit(acme, chart, server, settings):
    settings.FINDATA = {**settings.FINDATA, "SQL_MAX_ROWS": 1}
    response = pull(acme, chart, server, follow=True)
    shown = page_text(response)
    assert "more than 1 rows" in shown


def test_servers_switched_off_or_host_not_allowed(acme, chart, server, settings):
    settings.FINDATA = {**settings.FINDATA, "ALLOW_DB_CONNECT": False}
    assert pull(acme, chart, server).status_code == 403
    assert acme.get("/findata/connections/").status_code == 403
    settings.FINDATA = {
        **settings.FINDATA,
        "ALLOW_DB_CONNECT": True,
        "DB_ALLOWED_HOSTS": ["10.0.0.9"],
    }
    response = pull(acme, chart, server, follow=True)
    shown = page_text(response)
    assert "not on this site's list" in shown


def test_show_tables_lists_what_the_user_may_read(acme, chart, server):
    response = acme.post(
        f"/findata/{chart.id}/upload/database/tables/",
        {"connection": server.id, "password": PASSWORD},
    )
    names = [table.name for table in response.context["tables"]]
    assert "findata_dataset" in names


# ---------- saved connections ----------


def test_connections_are_for_staff_only(acme, settings):
    settings.FINDATA = {**settings.FINDATA, "CAN_EDIT_DEFINITIONS": lambda request: False}
    assert acme.get("/findata/connections/").status_code == 403
    assert acme.post("/findata/connections/new/", {"name": "x"}).status_code == 403


def test_save_test_and_edit_connection(acme, server):
    form = {
        "name": "Tally",
        "kind": "postgres",
        "host": server.host,
        "port": str(server.port),
        "database": server.database,
        "user": server.user,
        "password": PASSWORD,
    }
    response = acme.post("/findata/connections/test/", form)
    assert response.context["ok"], response.context["text"]
    assert response.context["text"].startswith("Connected.")
    assert acme.post("/findata/connections/new/", form).status_code == 302
    saved = DbConnection.objects.get(name="Tally")
    assert saved.port == server.port
    response = acme.post("/findata/connections/new/", form)
    assert "already a connection called" in response.context["error"]
    response = acme.post("/findata/connections/new/", form | {"name": "Bad", "host": "/tmp"})
    assert "host name or IP" in response.context["error"]


def test_delete_connection_forgets_pull_again(acme, chart, server):
    pull(acme, chart, server)
    upload = Upload.objects.latest("id")
    page = acme.get(f"/findata/connections/{server.id}/delete/")
    assert page.context["used_by"] == ["Chart of Accounts"]
    acme.post(f"/findata/connections/{server.id}/delete/")
    chart.refresh_from_db()
    upload.refresh_from_db()
    assert chart.remembered_source == {}
    assert (upload.connection, upload.file_name) == (None, "Local server")
    assert "Pull again" not in acme.get("/findata/").content.decode()


def test_other_company_cannot_use_a_connection(client, chart, server):
    session = client.session
    session["demo_company"] = "globex"
    session.save()
    other = Dataset.objects.create(company_key="globex", name="X", table_name="x")
    response = pull(client, other, server, follow=True)
    assert "Pick one of the saved connections." in page_text(response)
    assert not Upload.objects.filter(dataset=other).exists()


# ---------- values ----------


def test_database_values_keep_their_meaning(settings):
    assert db_source.plain_value(Decimal("1500.50")) == "1500.50"
    assert db_source.plain_value(True) == "Yes"
    assert db_source.plain_value(b"abc") == "(binary, 3 bytes)"
    noon_utc = datetime(2026, 4, 1, 6, 30, tzinfo=UTC)
    assert db_source.plain_value(noon_utc) == "2026-04-01 12:00:00"  # Asia/Kolkata
    assert db_source.plain_value({"a": 1}) == '{"a": 1}'


def test_typed_table_uses_the_databases_own_quotes():
    from data_core.databases import DIALECTS

    assert db_source.select_for("vouchers", "", None, DIALECTS["mysql"].quote) == (
        "SELECT * FROM `vouchers`"
    )
    assert db_source.select_for("sales.orders", "", None, DIALECTS["postgres"].quote) == (
        'SELECT * FROM "sales"."orders"'
    )


def test_test_connection_works_on_the_edit_form(acme, server):
    form = {
        "name": server.name,  # its own saved name: not "already a connection called"
        "kind": "postgres",
        "host": server.host,
        "port": str(server.port),
        "database": server.database,
        "user": server.user,
        "password": PASSWORD,
    }
    response = acme.post("/findata/connections/test/", form)
    assert response.context["ok"], response.context["text"]


def test_bad_connection_pick_is_explained(acme, chart, server):
    response = pull(acme, chart, server, follow=True, connection="abc")
    assert "Pick one of the saved connections." in page_text(response)
    response = acme.post(f"/findata/{chart.id}/upload/database/tables/", {"connection": "99999"})
    assert response.context["error"] == "Pick one of the saved connections."
