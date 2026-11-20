"""Phase 4: browse data, change log viewer, row history, recent uploads."""

import csv
import io
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from findata import browse, definition
from findata.models import Upload

from .conftest import col, define
from .test_upload import CHART_CSV, confirm, send

pytestmark = pytest.mark.django_db

ITEMS = {
    "name": "Items",
    "kind": "master",
    "description": "",
    "columns": [
        col("Item code", is_key=True),
        col("Item name"),
        col("Qty", "integer"),
        col("Price", "decimal"),
        col("Active", "boolean"),
        col("Since", "date"),
    ],
}
ITEMS_CSV = (
    "Item Code,Item Name,Qty,Price,Active,Since\n"
    "I3,Steel rod,5,99.50,No,03-04-2026\n"
    "I1,Copper wire,12,1500.25,Yes,01-04-2026\n"
    "I2,Domestic pipe,40,250,Yes,02-04-2026\n"
)


@pytest.fixture(autouse=True)
def upload_folder(settings, tmp_path):
    settings.FINDATA = {**settings.FINDATA, "UPLOAD_DIR": str(tmp_path)}


@pytest.fixture
def items(db):
    return define("acme", ITEMS)


def load(client, dataset, text, mode="add_update"):
    upload = send(client, dataset, text, mode=mode)[0]
    confirm(client, upload, delete_ok="yes")
    upload.refresh_from_db()
    assert upload.status == Upload.DONE, upload.problems
    return upload


def page_rows(response):
    return [row.cells for row in response.context["table"].page]


def first_cells(response):
    return [cells[0]["text"] for cells in page_rows(response)]


# ---------- Data tab ----------


def test_data_shows_rows_sorted_by_key(acme, items):
    load(acme, items, ITEMS_CSV)
    response = acme.get(f"/findata/{items.id}/")
    assert response.status_code == 200
    assert first_cells(response) == ["I1", "I2", "I3"]
    i1 = page_rows(response)[0]
    assert [cell["text"] for cell in i1] == [
        "I1",
        "Copper wire",
        "12",
        "1500.25",
        "Yes",
        "01-04-2026",
    ]
    assert b"Last changed" in response.content and b">History<" in response.content


@pytest.mark.parametrize(
    "query, expected",
    [
        ("item_name=dom", ["I2"]),
        ("qty_min=10&qty_max=40", ["I1", "I2"]),
        ("price_min=200", ["I1", "I2"]),
        ("since_min=2026-04-02", ["I2", "I3"]),
        ("active=False", ["I3"]),
        ("q=wire", ["I1"]),
        ("sort=-price", ["I1", "I2", "I3"]),
    ],
)
def test_data_filters(acme, items, query, expected):
    load(acme, items, ITEMS_CSV)
    assert first_cells(acme.get(f"/findata/{items.id}/?{query}")) == expected


def test_data_csv_is_in_template_order_and_uploads_back_unchanged(acme, items):
    load(acme, items, ITEMS_CSV)
    response = acme.get(f"/findata/{items.id}/?export=csv")
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    lines = list(csv.reader(io.StringIO(text)))
    assert lines[0] == ["Item code", "Item name", "Qty", "Price", "Active", "Since"]
    assert lines[1] == ["I1", "Copper wire", "12", "1500.25", "Yes", "2026-04-01"]
    again = send(acme, items, text)[0]
    assert (again.added, again.changed, again.deleted) == (0, 0, 0)


def test_data_empty_state(acme, items):
    response = acme.get(f"/findata/{items.id}/")
    assert b"No rows yet." in response.content
    assert f"/findata/{items.id}/upload/".encode() in response.content


def test_links_show_the_name_and_open_the_linked_row(acme, sales):
    chart = sales.columns.get(name="account_code").references
    load(acme, chart, CHART_CSV)
    load(
        acme, sales, "Voucher No,Line No,Voucher Date,Account Code,Amount\nS1,1,01-04-2026,A2,50\n"
    )
    cell = page_rows(acme.get(f"/findata/{sales.id}/"))[0][3]
    assert cell["text"] == "A2"
    assert cell["tip"].startswith("Bank")
    assert cell["url"].startswith(f"/findata/{chart.id}/?key=")
    linked = acme.get(cell["url"])
    assert first_cells(linked) == ["A2"]
    assert b"Showing one row" in linked.content


# ---------- periodic reports ----------


RECEIVABLES = {
    "name": "Receivables",
    "kind": "snapshot",
    "description": "",
    "columns": [col("Party", is_key=True), col("Amount", "decimal")],
}


def load_date(client, report, text, as_of):
    client.post(
        f"/findata/{report.id}/upload/",
        {"file": SimpleUploadedFile("rec.csv", text.encode()), "as_of": as_of},
    )
    upload = Upload.objects.latest("id")
    page = client.get(f"/findata/uploads/{upload.id}/map/")
    headers = page.context["headers_json"]
    form = {f"map_{i}": c for i, c in enumerate(page.context["chosen_json"])}
    form.update(sheet=page.context["sheet"].label, as_of=page.context["as_of"])
    assert len(headers) == len(page.context["chosen_json"])
    client.post(f"/findata/uploads/{upload.id}/map/", form)
    confirm(client, upload, delete_ok="yes")
    upload.refresh_from_db()
    assert upload.status == Upload.DONE, upload.problems
    return upload


def test_periodic_report_shows_one_date_at_a_time(acme, db):
    report = define("acme", RECEIVABLES)
    load_date(acme, report, "Party,Amount\nP1,100\nP2,200\n", "2026-07-31")
    load_date(acme, report, "Party,Amount\nP1,150\n", "2026-08-31")
    latest = acme.get(f"/findata/{report.id}/")
    assert latest.context["as_of"].isoformat() == "2026-08-31"
    assert [cells[1]["text"] for cells in page_rows(latest)] == ["150.00"]
    july = acme.get(f"/findata/{report.id}/?as_of=2026-07-31")
    assert first_cells(july) == ["P1", "P2"]
    nothing = acme.get(f"/findata/{report.id}/?as_of=2026-06-30")
    assert b"No rows for 30-06-2026." in nothing.content
    # a row's history covers one key on one date
    key = json.loads(page_rows(july) and july.context["table"].page[0].key_json)
    assert key == {"_as_of_date": "2026-07-31", "party": "P1"}
    history = acme.get(f"/findata/{report.id}/row-history/", {"key": json.dumps(key)})
    assert [event.action for event in history.context["events"]] == ["added"]
    changes = acme.get(f"/findata/{report.id}/changes/?as_of_min=2026-08-01")
    assert [(line.key_text, line.as_of.isoformat()) for line in changes.context["table"].page] == [
        ("P1", "2026-08-31")
    ]


# ---------- Changes tab ----------


def test_changes_show_only_changed_columns_and_one_upload(acme, chart):
    first = load(acme, chart, CHART_CSV)
    second = load(
        acme,
        chart,
        "Account Code,Account Name,Group,Opening Balance\nA1,Cash in hand,Assets,100\nA3,Loan,Liab,0\n",
        mode="full_refresh",
    )
    response = acme.get(f"/findata/{chart.id}/changes/")
    lines = list(response.context["table"].page)
    assert [(line.action, line.key_text) for line in lines] == [
        ("added", "A3"), ("changed", "A1"), ("deleted", "A2"), ("added", "A2"), ("added", "A1"),
    ]  # fmt: skip
    changed = lines[1].detail.changed
    assert [(item.label, item.old, item.new) for item in changed] == [
        ("Account name", "Cash", "Cash in hand")
    ]
    assert b"Cash in hand" in response.content
    only_first = acme.get(f"/findata/{chart.id}/changes/?upload={first.id}")
    assert {line.upload_id for line in only_first.context["table"].page} == {first.id}
    assert only_first.context["one_upload"] == first
    deleted = acme.get(f"/findata/{chart.id}/changes/?action=deleted")
    assert [line.key_text for line in deleted.context["table"].page] == ["A2"]
    assert second.changed == 1


def test_changes_csv_has_one_line_per_changed_column(acme, chart):
    load(acme, chart, CHART_CSV)
    load(acme, chart, "Account Code,Account Name,Group\nA1,Till,Cash\n")
    response = acme.get(f"/findata/{chart.id}/changes/?export=csv&action=changed")
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    lines = list(csv.reader(io.StringIO(text)))
    assert lines[0] == ["When", "By", "Upload", "Change", "Key", "Column", "Old", "New"]
    assert [line[3:] for line in lines[1:]] == [
        ["Changed", "A1", "Account name", "Cash", "Till"],
        ["Changed", "A1", "Group", "Assets", "Cash"],
    ]


def test_removed_column_and_undo_in_the_change_log(acme, chart):
    load(acme, chart, CHART_CSV)
    second = load(acme, chart, "Account Code,Account Name,Group\nA1,Cash,Bank\n")
    acme.post(f"/findata/uploads/{second.id}/undo/")
    second.refresh_from_db()
    assert second.status == Upload.UNDONE
    lines = list(acme.get(f"/findata/{chart.id}/changes/?action=changed").context["table"].page)
    assert lines[0].undone and "Undone on" in lines[0].undone_tip
    payload = definition.payload_of(chart)
    payload["columns"] = [column for column in payload["columns"] if column["name"] != "group_name"]
    chart = define("acme", payload, chart)
    lines = list(acme.get(f"/findata/{chart.id}/changes/?action=changed").context["table"].page)
    assert lines[0].detail.changed[0].label == "group_name (removed column)"


def test_empty_upload_says_nothing_changed(acme, chart):
    load(acme, chart, CHART_CSV)
    same = load(acme, chart, CHART_CSV)
    response = acme.get(f"/findata/{chart.id}/changes/?upload={same.id}")
    assert b"Nothing changed in this upload." in response.content
    assert acme.get(f"/findata/{chart.id}/changes/?upload=999999").status_code == 404


# ---------- row history ----------


def test_row_history_of_a_changed_then_deleted_row(acme, chart):
    load(acme, chart, CHART_CSV)
    load(
        acme,
        chart,
        "Account Code,Account Name,Group,Opening Balance\nA1,Cash,Assets,100\nA2,Bank,Assets,200\n",
    )
    undone = load(
        acme,
        chart,
        "Account Code,Account Name,Group,Opening Balance\nA1,Cash,Assets,100\n",
        mode="full_refresh",
    )
    acme.post(f"/findata/uploads/{undone.id}/undo/")
    response = acme.get(
        f"/findata/{chart.id}/row-history/", {"key": json.dumps({"account_code": "A2"})}
    )
    events = response.context["events"]
    assert [event.action for event in events] == ["added", "changed", "deleted", "undone"]
    change = events[1].detail.changed[0]
    assert (change.label, change.old, change.new) == ("Opening balance", "150000.50", "200.00")
    assert b"undo of" in response.content
    assert acme.get(f"/findata/{chart.id}/row-history/", {"key": "{bad"}).status_code == 404
    assert acme.get(f"/findata/{chart.id}/row-history/", {"key": '{"x": 1}'}).status_code == 404


def test_bad_as_of_in_a_row_key_is_not_found(acme, db):
    report = define("acme", RECEIVABLES)
    key = json.dumps({"party": "P1", "_as_of_date": "x"})
    assert acme.get(f"/findata/{report.id}/row-history/", {"key": key}).status_code == 404


def test_row_history_from_the_data_page_key(acme, items):
    load(acme, items, ITEMS_CSV)
    row = acme.get(f"/findata/{items.id}/").context["table"].page[0]
    assert json.loads(row.key_json) == {"item_code": "I1"}
    events = acme.get(f"/findata/{items.id}/row-history/", {"key": row.key_json}).context["events"]
    assert [event.action for event in events] == ["added"]
    assert ("Item name", "Copper wire") in [(v.label, v.new) for v in events[0].detail.values]


# ---------- recent uploads, tabs, isolation ----------


def test_recent_uploads_lists_this_company_only(acme, chart, client):
    first = load(acme, chart, CHART_CSV)
    other = define("globex", {**ITEMS, "name": "Globex items"})
    session = acme.session
    session["demo_company"] = "globex"
    session.save()
    load(acme, other, ITEMS_CSV)
    response = acme.get("/findata/uploads/")
    assert [upload.dataset.name for upload in response.context["table"].page] == ["Globex items"]
    session["demo_company"] = "acme"
    session.save()
    response = acme.get("/findata/uploads/")
    assert [upload.id for upload in response.context["table"].page] == [first.id]
    assert f"/findata/{chart.id}/changes/?upload={first.id}".encode() in response.content


def test_other_company_gets_404_and_viewers_can_browse(acme, chart, settings):
    upload = load(acme, chart, CHART_CSV)
    settings.FINDATA = {**settings.FINDATA, "CAN_UPLOAD": lambda request: False}
    for url in (
        f"/findata/{chart.id}/",
        f"/findata/{chart.id}/changes/",
        f"/findata/{chart.id}/history/",
    ):
        response = acme.get(url)
        assert response.status_code == 200, url
        assert f'href="/findata/{chart.id}/upload/"'.encode() not in response.content
    session = acme.session
    session["demo_company"] = "globex"
    session.save()
    for url in (
        f"/findata/{chart.id}/",
        f"/findata/{chart.id}/changes/",
        f"/findata/{chart.id}/changes/?upload={upload.id}",
        f"/findata/{chart.id}/row-history/?key=%7B%22account_code%22%3A%22A1%22%7D",
    ):
        assert acme.get(url).status_code == 404, url


def test_dataset_list_links_to_the_dataset_page(acme, chart):
    response = acme.get("/findata/")
    assert f'href="/findata/{chart.id}/"'.encode() in response.content
    assert b"Recent uploads" in response.content
    tabs = acme.get(f"/findata/{chart.id}/history/")
    assert b'role="tablist"' in tabs.content and b">Changes</a>" in tabs.content


def test_browse_models_handle_odd_column_names(db):
    dataset = define(
        "acme",
        {
            "name": "Odd",
            "kind": "master",
            "description": "",
            "columns": [col("Code", is_key=True), {**col("Id"), "name": "id"}],
        },
    )
    model = browse.row_model(dataset)
    names = {field.name for field in model._meta.fields}
    assert "code" in names and f"col{dataset.columns.get(name='id').id}" in names
    assert list(browse.rows(dataset)) == []
