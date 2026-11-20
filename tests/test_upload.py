import copy
import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from findata import definition, mapping, values
from findata import upload as uploads
from findata.models import DatasetColumn, Upload
from findata.schema_builder import ColumnSpec, SchemaBlocked

from .conftest import col, define, sql

pytestmark = pytest.mark.django_db

CHART_CSV = 'Account Code,Account Name,Group,Opening Balance\nA1,Cash,Assets,100\nA2,Bank,Assets,"1,50,000.50"\n'


@pytest.fixture(autouse=True)
def upload_folder(settings, tmp_path):
    settings.FINDATA = {**settings.FINDATA, "UPLOAD_DIR": str(tmp_path)}


def send(client, dataset, text, mode="add_update", name="data.csv", override=None):
    """Upload a CSV through the screens; returns (upload, response of the mapping POST)."""
    response = client.post(
        f"/findata/{dataset.id}/upload/", {"file": SimpleUploadedFile(name, text.encode())}
    )
    assert response.status_code == 302, response.content
    upload = Upload.objects.latest("id")
    page = client.get(f"/findata/uploads/{upload.id}/map/")
    headers = page.context["headers_json"]
    chosen = dict(zip(headers, page.context["chosen_json"], strict=True))
    chosen.update(override or {})
    form = {f"map_{index}": chosen[header] for index, header in enumerate(headers)}
    form.update(mode=mode, sheet=page.context["sheet"].label)
    response = client.post(f"/findata/uploads/{upload.id}/map/", form)
    upload.refresh_from_db()
    return upload, response


def confirm(client, upload, **extra):
    return client.post(f"/findata/uploads/{upload.id}/confirm/", extra, follow=True)


def chart_rows():
    return sql(
        "SELECT account_code, account_name, group_name, opening_balance "
        "FROM co_acme.chart_of_accounts ORDER BY 1"
    )


def texts(upload):
    return " | ".join(item["text"] for item in upload.problems)


# ---------- values ----------


def spec(data_type, places=2):
    return ColumnSpec(1, "x", "X", data_type, places, False, False, False, None)


@pytest.mark.parametrize(
    "cell, data_type, expected",
    [
        ("1,50,000.50", "decimal", Decimal("150000.50")),
        (1500.5, "decimal", Decimal("1500.50")),
        ("12", "integer", 12),
        (12.0, "integer", 12),
        ("01-04-2026", "date", date(2026, 4, 1)),
        ("2026-04-01", "date", date(2026, 4, 1)),
        ("Y", "boolean", True),
        ("0", "boolean", False),
        (1001, "text", "1001"),
        ("  Cash ", "text", "Cash"),
        ("", "text", None),
    ],
)
def test_values_convert(cell, data_type, expected):
    assert values.convert(cell, spec(data_type)) == expected


@pytest.mark.parametrize(
    "cell, data_type, message",
    [
        ("abc", "decimal", "isn't a number"),
        ("1,5", "decimal", "isn't a number"),  # a decimal comma is not read as 15
        (1e30, "decimal", "is too large"),
        ("12.5", "integer", "has decimals"),
        ("31-31-2026", "date", "isn't a date"),
        ("maybe", "boolean", "isn't Yes / No"),
    ],
)
def test_values_explain_bad_cells(cell, data_type, message):
    with pytest.raises(values.BadValue, match=message):
        values.convert(cell, spec(data_type))


# ---------- mapping ----------


def test_mapping_guess_exact_close_and_remembered(chart):
    guessed = mapping.guess(chart, ["ACCOUNT CODE", "Acount Name", "ledg_grp", "Notes"])
    assert guessed == {
        "ACCOUNT CODE": "account_code",
        "Acount Name": "account_name",
        "ledg_grp": "",
        "Notes": "",
    }
    mapping.remember(chart, {"ledg_grp": "group_name", "Notes": ""})
    chart.refresh_from_db()
    assert mapping.guess(chart, ["ledg_grp", "Notes"]) == {"ledg_grp": "group_name", "Notes": ""}


def test_mapping_needs_required_columns(chart):
    found = mapping.problems(chart, {"Code": "account_code", "Other": ""})
    assert len(found) == 1
    assert found[0].startswith("Account name is required but isn't in this file.")
    twice = mapping.problems(chart, {"a": "account_code", "b": "account_code", "c": "account_name"})
    assert "picked for 2 file columns" in twice[0]


def test_remembering_a_mapping_does_not_count_as_a_definition_change(chart):
    before = chart.updated_at
    mapping.remember(chart, {"x": ""})
    chart.refresh_from_db()
    assert chart.updated_at == before


# ---------- add / update ----------


def test_add_update_preview_confirm_and_history(acme, chart):
    upload, response = send(acme, chart, CHART_CSV)
    assert response.status_code == 302 and upload.status == Upload.PREVIEW
    assert (upload.added, upload.changed, upload.deleted) == (2, 0, 0)
    page = acme.get(f"/findata/uploads/{upload.id}/")
    assert "Check before saving" in page.content.decode()
    assert [row["account_code"] for row in page.context["table"].page] == ["A1", "A2"]
    assert chart_rows() == []  # nothing saved before confirm

    confirm(acme, upload)
    assert chart_rows() == [
        ("A1", "Cash", "Assets", Decimal("100.00")),
        ("A2", "Bank", "Assets", Decimal("150000.50")),
    ]
    upload.refresh_from_db()
    assert upload.status == Upload.DONE
    assert "data.csv" in acme.get(f"/findata/{chart.id}/history/").content.decode()
    assert sql("SELECT to_regclass(%s)", [f"co_acme._stage_{upload.id}"]) == [(None,)]

    second, _ = send(acme, chart, "Account Code,Account Name\nA1,Cash in hand\nA3,Petty cash\n")
    assert (second.added, second.changed, second.unchanged) == (1, 1, 0)
    changed = acme.get(f"/findata/uploads/{second.id}/?tab=changed").context["table"].page
    assert changed[0]["account_name"] == "Cash → Cash in hand"
    confirm(acme, second)
    # Group and Opening balance weren't in the file: saved rows keep them.
    assert chart_rows()[0] == ("A1", "Cash in hand", "Assets", Decimal("100.00"))
    assert chart_rows()[2] == ("A3", "Petty cash", None, None)


def test_keys_ignore_capitals_and_keep_the_saved_spelling(acme, chart):
    confirm(acme, send(acme, chart, CHART_CSV)[0])
    upload, _ = send(acme, chart, "Account Code,Account Name\na1,Cash renamed\n")
    assert (upload.added, upload.changed) == (0, 1)
    confirm(acme, upload)
    assert chart_rows()[0][:2] == ("A1", "Cash renamed")


def test_look_alike_keys_in_one_file_are_refused(acme, chart):
    upload, response = send(acme, chart, "Account Code,Account Name\nab,One\nAB,Two\n")
    assert response.url == f"/findata/uploads/{upload.id}/problems/"
    assert "Key “ab” is in rows 2, 3" in texts(upload) or "Key “AB” is in rows 2, 3" in texts(
        upload
    )
    page = acme.get(response.url).content.decode()
    assert "can't be saved yet" in page


def test_bad_cells_and_blank_keys_are_listed_with_rows(acme, chart):
    upload, _ = send(
        acme, chart, "Account Code,Account Name,Opening Balance\nA1,Cash,abc\n,Bank,5\nA3,,1\n"
    )
    found = texts(upload)
    assert "Opening balance: “abc” isn't a number" in found
    assert "Account code is blank, but it's required." in found
    assert [item["row"] for item in upload.problems] == [2, 3, 4]
    excel = acme.get(f"/findata/uploads/{upload.id}/problems.xlsx")
    assert excel["Content-Disposition"].endswith('_problems.xlsx"')


def test_missing_required_column_blocks_at_mapping(acme, chart):
    upload, response = send(acme, chart, "Account Code,Group\nA1,Assets\n")
    assert response.status_code == 400
    assert "Account name is required but isn't in this file" in response.context["problems"][0]
    assert upload.status == Upload.MAPPING


# ---------- links ----------


def test_links_match_without_capitals_and_use_the_master_spelling(acme, sales):
    chart = sales.columns.get(name="account_code").references
    confirm(acme, send(acme, chart, CHART_CSV)[0])
    text = "Voucher No,Line No,Voucher Date,Account Code,Amount\nS1,1,01-04-2026,a1,500\n"
    confirm(acme, send(acme, sales, text)[0])
    assert sql("SELECT account_code, voucher_date FROM co_acme.sales") == [("A1", date(2026, 4, 1))]

    bad, _ = send(
        acme,
        sales,
        "Voucher No,Line No,Voucher Date,Account Code,Amount\n"
        "S2,1,01-04-2026,A1,abc\nS2,2,01-04-2026,4010,5\n",
    )
    # Bad cells and broken links are listed together, in row order.
    assert [item["row"] for item in bad.problems] == [2, 3]
    assert (
        "Account code “4010” is not in Chart of Accounts. Upload Chart of Accounts first."
        in texts(bad)
    )


def test_full_refresh_cannot_delete_a_row_that_is_still_used(acme, sales):
    chart = sales.columns.get(name="account_code").references
    confirm(acme, send(acme, chart, CHART_CSV)[0])
    confirm(
        acme,
        send(acme, sales, "Voucher No,Line No,Voucher Date,Account Code\nS1,1,01-04-2026,A2\n")[0],
    )
    upload, _ = send(acme, chart, "Account Code,Account Name\nA1,Cash\n", mode="full_refresh")
    assert "“A2” isn't in the file, so Full refresh would delete it, but Sales uses it" in texts(
        upload
    )


# ---------- full refresh ----------


def test_full_refresh_deleting_most_rows_needs_the_tick(acme, chart):
    confirm(acme, send(acme, chart, CHART_CSV + "A3,Petty,Assets,1\n")[0])
    upload, _ = send(acme, chart, "Account Code,Account Name\nA1,Cash\n", mode="full_refresh")
    assert (upload.deleted, upload.existing) == (2, 3)
    assert upload.needs_delete_tick
    response = confirm(acme, upload)
    assert "Tick “Yes, delete 2 rows”" in response.content.decode()
    assert len(chart_rows()) == 3
    confirm(acme, upload, delete_ok="yes")
    assert [row[0] for row in chart_rows()] == ["A1"]


# ---------- change log and undo ----------


def test_change_log_and_undo_put_everything_back(acme, chart):
    confirm(acme, send(acme, chart, CHART_CSV)[0])
    before = chart_rows()
    upload, _ = send(
        acme, chart, "Account Code,Account Name\nA1,Cash new\nA9,New one\n", mode="full_refresh"
    )
    confirm(acme, upload, delete_ok="yes")
    log = sql(
        "SELECT action, key->>'account_code' FROM co_acme._changes WHERE upload_id = %s ORDER BY 1",
        [upload.id],
    )
    assert log == [("added", "A9"), ("changed", "A1"), ("deleted", "A2")]

    page = acme.get(f"/findata/uploads/{upload.id}/undo/")
    assert page.context["problems"] == []
    acme.post(f"/findata/uploads/{upload.id}/undo/")
    assert chart_rows() == before
    upload.refresh_from_db()
    assert upload.status == Upload.UNDONE


def test_only_the_latest_upload_can_be_undone(acme, chart):
    first = send(acme, chart, CHART_CSV)[0]
    confirm(acme, first)
    confirm(acme, send(acme, chart, "Account Code,Account Name\nA5,Five\n")[0])
    first.refresh_from_db()
    assert "Only the latest upload" in uploads.undo_problems(first)[0]


def test_undo_is_blocked_after_a_definition_change(acme, chart):
    upload = send(acme, chart, CHART_CSV)[0]
    confirm(acme, upload)
    payload = definition.payload_of(chart)
    payload["columns"].append(col("Notes"))
    define("acme", payload, chart)
    upload.refresh_from_db()
    assert "definition was changed" in uploads.undo_problems(upload)[0]


def test_undo_is_blocked_when_another_dataset_uses_added_rows(acme, sales):
    chart = sales.columns.get(name="account_code").references
    upload = send(acme, chart, CHART_CSV)[0]
    confirm(acme, upload)
    confirm(
        acme,
        send(acme, sales, "Voucher No,Line No,Voucher Date,Account Code\nS1,1,01-04-2026,A1\n")[0],
    )
    upload.refresh_from_db()
    assert "Sales now uses rows this upload added (e.g. “A1”" in uploads.undo_problems(upload)[0]


# ---------- safety ----------


def test_preview_is_refused_when_data_changed_since(acme, chart):
    first = send(acme, chart, CHART_CSV)[0]
    second = send(acme, chart, "Account Code,Account Name\nB1,Other\n")[0]
    confirm(acme, first)
    response = confirm(acme, second)
    assert "was changed by another upload after this preview" in response.content.decode()
    assert len(chart_rows()) == 2
    # Back at the mapping, so one click gives a fresh preview.
    second.refresh_from_db()
    assert second.status == Upload.MAPPING
    assert response.redirect_chain[-1][0] == f"/findata/uploads/{second.id}/map/"


def test_older_tables_get_the_capitals_rule_at_their_first_upload(acme, chart):
    sql(f"DROP INDEX co_acme.fd_ci_pk_{chart.id}")
    sql(
        "INSERT INTO co_acme.chart_of_accounts (account_code, account_name) "
        "VALUES ('ab', 'x'), ('AB', 'y')"
    )
    upload, _ = send(acme, chart, CHART_CSV)
    assert "differ only in capitals" in texts(upload)
    sql("DELETE FROM co_acme.chart_of_accounts WHERE account_code = 'AB'")
    upload, _ = send(acme, chart, CHART_CSV)
    assert upload.status == Upload.PREVIEW
    assert sql(
        "SELECT count(*) FROM pg_indexes WHERE indexname = %s", [f"fd_ci_pk_{chart.id}"]
    ) == [(1,)]


def test_cancel_leaves_nothing_behind(acme, chart):
    upload = send(acme, chart, CHART_CSV)[0]
    acme.post(f"/findata/uploads/{upload.id}/cancel/")
    upload.refresh_from_db()
    assert upload.status == Upload.CANCELLED
    assert not uploads.file_path(upload).exists()
    assert sql("SELECT to_regclass(%s)", [f"co_acme._stage_{upload.id}"]) == [(None,)]


def test_back_from_preview_keeps_the_file(acme, chart):
    upload = send(acme, chart, CHART_CSV)[0]
    assert acme.get(f"/findata/uploads/{upload.id}/map/").status_code == 200
    upload.refresh_from_db()
    assert upload.status == Upload.MAPPING and uploads.file_path(upload).exists()


def test_other_company_cannot_touch_an_upload(client, acme, chart):
    upload = send(acme, chart, CHART_CSV)[0]
    session = acme.session
    session["demo_company"] = "globex"
    session.save()
    assert acme.get(f"/findata/uploads/{upload.id}/").status_code == 404
    assert acme.post(f"/findata/uploads/{upload.id}/confirm/").status_code == 404


def test_upload_permission_hook(acme, chart, settings):
    confirm(acme, send(acme, chart, CHART_CSV)[0])
    settings.FINDATA = {**settings.FINDATA, "CAN_UPLOAD": lambda request: False}
    response = acme.get(f"/findata/{chart.id}/upload/")
    assert response.status_code == 403
    assert b"upload or undo" in response.content
    history = acme.get(f"/findata/{chart.id}/history/")
    assert not any(row["can_undo"] for row in history.context["table"].page)


def test_unique_column_ignores_capitals_in_the_definition_check(chart):
    sql(
        "INSERT INTO co_acme.chart_of_accounts (account_code, account_name) "
        "VALUES ('1', 'Cash'), ('2', 'CASH')"
    )
    payload = definition.payload_of(chart)
    payload["columns"][1]["unique"] = True
    checked = definition.check("acme", copy.deepcopy(payload), chart)
    with pytest.raises(
        SchemaBlocked, match="Account name. has values that differ only in capitals"
    ):
        definition.save("acme", checked, chart)


def test_database_refuses_look_alike_keys(chart):
    sql("INSERT INTO co_acme.chart_of_accounts (account_code, account_name) VALUES ('ab', 'x')")
    with pytest.raises(Exception, match="fd_ci_pk"):
        sql("INSERT INTO co_acme.chart_of_accounts (account_code, account_name) VALUES ('AB', 'y')")


def test_deleting_a_dataset_clears_its_change_log(acme, chart):
    confirm(acme, send(acme, chart, CHART_CSV)[0])
    acme.post(f"/findata/{chart.id}/delete/")
    assert sql("SELECT count(*) FROM co_acme._changes") == [(0,)]


def test_types_match_column_choices():
    assert set(values.CONVERTERS) == {value for value, _ in DatasetColumn.TYPE_CHOICES}


def test_text_codes_stay_as_typed(acme, chart):
    upload, _ = send(acme, chart, "Account Code,Account Name,Opening Balance\n1.10,Cash,1500.5\n")
    confirm(acme, upload)
    assert chart_rows() == [("1.10", "Cash", None, Decimal("1500.50"))]


def test_problem_rows_are_the_rows_in_the_excel_sheet(acme, chart):
    workbook = Workbook()
    sheet = workbook.active
    for row in ([], ["Account Code", "Account Name"], ["A1", "Cash"], [], ["A2", ""]):
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    response = acme.post(
        f"/findata/{chart.id}/upload/", {"file": SimpleUploadedFile("c.xlsx", buffer.getvalue())}
    )
    upload = Upload.objects.latest("id")
    page = acme.get(response.url)
    form = {f"map_{i}": name for i, name in enumerate(page.context["chosen_json"])}
    form.update(mode="add_update", sheet=page.context["sheet"].label)
    acme.post(response.url, form)
    upload.refresh_from_db()
    assert upload.problems == [
        {"row": 5, "text": "Account name is blank, but it's required.", "key": "A2"}
    ]
