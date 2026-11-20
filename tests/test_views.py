import io
import json

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook, load_workbook

from findata import definition
from findata.models import Dataset

from .conftest import CHART, sql

pytestmark = pytest.mark.django_db


def post_definition(client, url, payload):
    return client.post(url, {"definition": json.dumps(payload)})


def test_no_company_shows_a_friendly_page(client):
    response = client.get("/findata/")
    assert b"Pick a company first" in response.content


def test_missing_hook_gives_a_clear_message(client, settings):
    settings.FINDATA = {}
    with pytest.raises(ImproperlyConfigured, match="needs FINDATA\\['GET_COMPANY'\\]"):
        client.get("/findata/")


def test_bad_company_key_from_host_is_explained(client, settings):
    settings.FINDATA = {"GET_COMPANY": lambda request: ("ACME Ltd", "ACME Ltd")}
    with pytest.raises(ImproperlyConfigured, match="must start with a small letter"):
        client.get("/findata/")


def test_default_permission_is_staff_only(acme, settings):
    settings.FINDATA = {**settings.FINDATA, "CAN_EDIT_DEFINITIONS": None}
    response = acme.get("/findata/new/")
    assert response.status_code == 403
    assert b"only people allowed" in response.content
    assert acme.get("/findata/").status_code == 200


def test_create_from_editor_and_list(acme):
    response = post_definition(acme, "/findata/new/save/", CHART)
    assert response.status_code == 302
    dataset = Dataset.objects.get(company_key="acme")
    assert dataset.columns.count() == 4
    page = acme.get("/findata/").content.decode()
    assert "Chart of Accounts" in page
    assert "co_acme.chart_of_accounts" in page


def test_editor_shows_problems_and_keeps_what_was_typed(acme):
    payload = {**CHART, "columns": [{**CHART["columns"][1]}]}  # no key
    response = post_definition(acme, "/findata/new/save/", payload)
    assert response.status_code == 400
    assert "Tick Key" in response.content.decode()
    assert "Account name" in response.content.decode()
    assert not Dataset.objects.exists()


def test_edit_blocked_change_is_shown(acme, chart):
    sql("INSERT INTO co_acme.chart_of_accounts (account_code, account_name) VALUES ('1', 'Cash')")
    payload = definition.payload_of(chart)
    payload["columns"][2]["required"] = True
    response = post_definition(acme, f"/findata/{chart.id}/edit/", payload)
    assert response.status_code == 400
    assert "“Group” is blank in 1 row(s)" in response.content.decode()


def test_other_companys_dataset_is_not_found(acme):
    from .conftest import define

    globex = define("globex", CHART)
    assert acme.get(f"/findata/{globex.id}/edit/").status_code == 404
    assert acme.post(f"/findata/{globex.id}/delete/").status_code == 404
    assert "Chart of Accounts" not in acme.get("/findata/").content.decode()


def test_delete_blocked_by_link_then_allowed(acme, sales):
    chart = sales.columns.get(name="account_code").references
    response = acme.post(f"/findata/{chart.id}/delete/", follow=True)
    assert "Sales link(s) to it" in response.content.decode()
    assert Dataset.objects.filter(pk=chart.pk).exists()

    acme.post(f"/findata/{sales.id}/delete/")
    acme.post(f"/findata/{chart.id}/delete/")
    assert not Dataset.objects.exists()
    assert sql("SELECT to_regclass('co_acme.sales'), to_regclass('co_acme.chart_of_accounts')") == [
        (None, None)
    ]


def test_template_download(acme, sales):
    response = acme.get(f"/findata/{sales.id}/template/")
    assert response["Content-Disposition"] == 'attachment; filename="sales_template.xlsx"'
    workbook = load_workbook(io.BytesIO(response.content))
    headers = [cell.value for cell in workbook["Data"][1]]
    assert headers == ["Voucher no", "Line no", "Voucher date", "Account code", "Amount"]
    assert "Must exist in Chart of Accounts" in workbook["Data"]["D1"].comment.text
    assert workbook["Help"]["G5"].value == "Chart of Accounts"


def sample_xlsx():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ledgers"
    sheet.append(["Account Code", "Account Name", "Opening Balance", "Active"])
    sheet.append(["01001", "Cash", 1500.5, "Yes"])
    sheet.append(["01002", "Bank", 22000, "No"])
    sheet.append(["01003", "Cash", 0, "Yes"])
    lines = workbook.create_sheet("Lines")
    lines.append(["Voucher No", "Line No", "Date", "Amount"])
    lines.append(["S1", 1, "01-04-2026", 100])
    lines.append(["S1", 2, "01-04-2026", 50])
    lines.append(["S2", 1, "02-04-2026", 75])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_sample_file_guesses_columns_types_and_key(acme):
    upload = SimpleUploadedFile("tally.xlsx", sample_xlsx())
    response = acme.post("/findata/new/sample/", {"sample": upload})
    assert response.status_code == 200
    sheets = response.context["sheets_json"]
    assert [sheet["name"] for sheet in sheets] == ["Ledgers", "Lines"]

    ledgers = {c["name"]: c for c in sheets[0]["columns"]}
    assert ledgers["account_code"]["data_type"] == "text"  # leading zeros kept
    assert ledgers["account_code"]["is_key"] is True
    assert ledgers["opening_balance"]["data_type"] == "decimal"
    assert ledgers["active"]["data_type"] == "boolean"
    assert "Suggested key: Account Code" in sheets[0]["key_note"]

    lines = {c["name"]: c for c in sheets[1]["columns"]}
    assert lines["voucher_no"]["is_key"] and lines["line_no"]["is_key"]
    assert lines["date"]["data_type"] == "date"
    assert not Dataset.objects.exists()  # nothing saved yet


def test_bad_sample_file_is_explained(acme):
    upload = SimpleUploadedFile("notes.pdf", b"%PDF-1.4")
    response = acme.post("/findata/new/sample/", {"sample": upload}, follow=True)
    assert "notes.pdf: Only CSV, XLSX" in response.content.decode()


def test_copy_from_other_company(acme):
    from .conftest import define

    globex = define("globex", CHART)
    page = acme.get("/findata/new/").content.decode()
    assert "Globex Pvt Ltd" in page
    response = acme.get(f"/findata/new/copy/{globex.id}/")
    assert response.context["payload_json"]["name"] == "Chart of Accounts"


def test_host_returning_a_wrong_shape_is_explained(client, settings):
    settings.FINDATA = {"GET_COMPANY": lambda request: "acme"}
    with pytest.raises(ImproperlyConfigured, match="expects a pair"):
        client.get("/findata/")


def test_key_guess_ignores_words_that_only_end_like_id(acme):
    csv = "Amount Paid,Invoice No,Party\nA1,INV1,P\nA2,INV2,P\n"
    upload = SimpleUploadedFile("bills.csv", csv.encode())
    columns = acme.post("/findata/new/sample/", {"sample": upload}).context["sheets_json"][0][
        "columns"
    ]
    assert [c["name"] for c in columns if c["is_key"]] == ["invoice_no"]


def test_json_sample_is_refused(acme):
    upload = SimpleUploadedFile("data.json", b'[{"a": 1}]')
    response = acme.post("/findata/new/sample/", {"sample": upload}, follow=True)
    assert "Use a CSV or Excel file as the sample." in response.content.decode()
