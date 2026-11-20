"""Phase 3: periodic reports (As-of dates): periods, upload a date, replace, remove, calendar."""

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from findata import mapping, periods
from findata import upload as uploads
from findata.models import Dataset, Upload

from .conftest import col, define, sql

D = date.fromisoformat


# ---------- periods (no database) ----------


@pytest.mark.parametrize(
    "how_often, start, end, expected",
    [
        ("month_end", "2024-01-15", "2024-03-31", ["2024-01-31", "2024-02-29", "2024-03-31"]),
        ("month_end", "2026-02-01", "2026-02-28", ["2026-02-28"]),
        ("quarter_end", "2026-01-01", "2026-12-31",
         ["2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31"]),
        ("weekdays", "2026-11-13", "2026-11-17", ["2026-11-13", "2026-11-16", "2026-11-17"]),
        ("mon_sat", "2026-11-13", "2026-11-16", ["2026-11-13", "2026-11-14", "2026-11-16"]),
        ("daily", "2026-11-14", "2026-11-15", ["2026-11-14", "2026-11-15"]),
        ("any", "2026-01-01", "2026-12-31", []),
    ],
)  # fmt: skip
def test_expected_dates(how_often, start, end, expected):
    assert periods.expected_dates(how_often, D(start), D(end)) == [D(day) for day in expected]


def test_missing_counts_from_the_first_loaded_date():
    loaded = [D("2026-01-31"), D("2026-03-31")]
    assert periods.missing_dates("month_end", loaded, D("2026-04-20")) == [D("2026-02-28")]
    assert periods.missing_dates("month_end", [], D("2026-04-20")) == []
    assert periods.missing_dates("any", loaded, D("2026-04-20")) == []


def test_suggested_date():
    today = D("2026-04-20")
    assert periods.suggested_date("month_end", [D("2026-01-31")], today) == (D("2026-02-28"), True)
    up_to_date = [D("2026-02-28"), D("2026-03-31")]
    assert periods.suggested_date("month_end", up_to_date, today) == (D("2026-03-31"), False)
    assert periods.suggested_date("month_end", [], today) == (D("2026-03-31"), True)
    assert periods.suggested_date("any", up_to_date, today) == (today, False)


# ---------- uploads (database) ----------


RECEIVABLES = {
    "name": "Receivables",
    "kind": "snapshot",
    "description": "",
    "columns": [col("Party", is_key=True), col("Amount", "decimal"), col("Note", unique=True)],
}


@pytest.fixture(autouse=True)
def upload_folder(settings, tmp_path):
    settings.FINDATA = {**settings.FINDATA, "UPLOAD_DIR": str(tmp_path)}


@pytest.fixture
def report(db):
    return define("acme", RECEIVABLES)


def send(client, dataset, text, as_of="", map_as_of=None, map_date=None):
    """Upload a CSV with an As-of date; returns (upload, response of the mapping POST).
    `map_as_of` = the file column that has the date; `map_date` = the date typed on the
    mapping page (default: what the upload page sent)."""
    response = client.post(
        f"/findata/{dataset.id}/upload/",
        {"file": SimpleUploadedFile("rec.csv", text.encode()), "as_of": as_of},
    )
    if response.status_code != 302:
        return None, response
    upload = Upload.objects.latest("id")
    page = client.get(f"/findata/uploads/{upload.id}/map/")
    headers = page.context["headers_json"]
    chosen = dict(zip(headers, page.context["chosen_json"], strict=True))
    if map_as_of:
        chosen[map_as_of] = "_as_of_date"
    form = {f"map_{index}": chosen[header] for index, header in enumerate(headers)}
    form.update(sheet=page.context["sheet"].label, as_of=page.context["as_of"])
    if map_date is not None:
        form["as_of"] = map_date
    response = client.post(f"/findata/uploads/{upload.id}/map/", form)
    upload.refresh_from_db()
    return upload, response


def confirm(client, upload):
    return client.post(f"/findata/uploads/{upload.id}/confirm/", {"delete_ok": "yes"}, follow=True)


def saved():
    return sql(
        "SELECT _as_of_date::text, party, amount::text FROM co_acme.receivables ORDER BY 1, 2"
    )


MARCH = "Party,Amount\nP1,100\nP2,200\nP3,300\n"
FEB = "Party,Amount\nP1,90\nP2,190\n"


@pytest.mark.django_db
def test_upload_a_new_date_then_replace_it_only(acme, report):
    upload, response = send(acme, report, FEB, as_of="2026-02-28")
    assert response.status_code == 302, response.content
    assert upload.mode == Upload.DATES
    assert upload.dates == [
        {"date": "2026-02-28", "rows": 2, "added": 2, "changed": 0, "deleted": 0, "existing": 0}
    ]
    confirm(acme, upload)
    confirm(acme, send(acme, report, MARCH, as_of="2026-03-31")[0])

    corrected = "Party,Amount\nP1,150\nP2,200\nP4,400\n"  # P1 changed, P3 gone, P4 new
    upload, _ = send(acme, report, corrected, as_of="2026-03-31")
    assert (upload.added, upload.changed, upload.deleted, upload.existing) == (1, 1, 1, 3)
    page = acme.get(f"/findata/uploads/{upload.id}/")
    assert b"31-03-2026" in page.content
    confirm(acme, upload)
    assert saved() == [
        ("2026-02-28", "P1", "90.00"),
        ("2026-02-28", "P2", "190.00"),  # another date: untouched
        ("2026-03-31", "P1", "150.00"),
        ("2026-03-31", "P2", "200.00"),
        ("2026-03-31", "P4", "400.00"),
    ]


@pytest.mark.django_db
def test_undo_puts_the_replaced_date_back(acme, report):
    confirm(acme, send(acme, report, MARCH, as_of="2026-03-31")[0])
    before = saved()
    upload, _ = send(acme, report, "Party,Amount\nP1,1\n", as_of="2026-03-31")
    confirm(acme, upload)
    assert len(saved()) == 1
    acme.post(f"/findata/uploads/{upload.id}/undo/")
    assert saved() == before


@pytest.mark.django_db
def test_many_dates_from_a_file_column(acme, report):
    text = "Report date,Party,Amount\n31-01-2026,P1,10\n31-01-2026,P2,20\n28-02-2026,P1,11\n"
    upload, response = send(acme, report, text, map_as_of="Report date")
    assert response.status_code == 302, response.content
    assert [item["date"] for item in upload.dates] == ["2026-01-31", "2026-02-28"]
    assert upload.as_of_date is None
    confirm(acme, upload)
    assert len(saved()) == 3


@pytest.mark.django_db
def test_report_date_column_is_guessed(report):
    assert mapping.guess(report, ["Report Date", "Party"])["Report Date"] == "_as_of_date"


@pytest.mark.django_db
def test_bad_and_future_dates_in_a_column_are_problems(acme, report):
    future = (timezone.localdate() + timedelta(days=40)).strftime("%d-%m-%Y")
    text = f"Report date,Party,Amount\n,P1,10\nsoon,P2,20\n{future},P3,30\n"
    upload, response = send(acme, report, text, map_as_of="Report date")
    assert response.status_code == 302
    problems = {item["row"]: item for item in upload.problems}
    assert "As-of date is blank" in problems[2]["text"]
    assert problems[2]["key"] == "P1"
    assert "As-of date" in problems[3]["text"]
    assert "in the future" in problems[4]["text"]


@pytest.mark.django_db
def test_future_picked_date_is_refused(acme, report):
    later = (timezone.localdate() + timedelta(days=3)).isoformat()
    _, response = send(acme, report, MARCH, as_of=later)
    assert response.status_code == 400
    assert b"is in the future" in response.content


@pytest.mark.django_db
def test_mapping_needs_a_date_or_a_column(acme, report):
    upload, response = send(acme, report, MARCH, as_of="", map_date="")
    assert response.status_code == 400
    assert "Pick the As-of date" in response.content.decode()
    assert upload.status == Upload.MAPPING


@pytest.mark.django_db
def test_unique_columns_are_unique_per_date(acme, report):
    confirm(acme, send(acme, report, "Party,Note\nP1,n1\n", as_of="2026-02-28")[0])
    upload, _ = send(acme, report, "Party,Note\nP1,n1\n", as_of="2026-03-31")
    assert upload.status == Upload.PREVIEW, upload.problems
    upload, _ = send(acme, report, "Party,Note\nP1,n1\nP2,N1\n", as_of="2026-01-31")
    assert "must be unique" in upload.problems[0]["text"]


@pytest.mark.django_db
def test_remove_a_date_and_undo_it(acme, report):
    confirm(acme, send(acme, report, FEB, as_of="2026-02-28")[0])
    confirm(acme, send(acme, report, MARCH, as_of="2026-03-31")[0])
    page = acme.get(f"/findata/{report.id}/dates/2026-03-31/remove/")
    assert b"3</strong> rows" in page.content
    acme.post(f"/findata/{report.id}/dates/2026-03-31/remove/")
    assert [row[0] for row in saved()] == ["2026-02-28", "2026-02-28"]
    removal = Upload.objects.latest("id")
    assert (removal.mode, removal.deleted, removal.file_name) == (
        Upload.REMOVE_DATE, 3, "Removed 31-03-2026",
    )  # fmt: skip
    history = acme.get(f"/findata/{report.id}/history/").content.decode()
    assert "Removed 31-03-2026" in history and "Remove a date" in history
    acme.post(f"/findata/uploads/{removal.id}/undo/")
    assert len(saved()) == 5


@pytest.mark.django_db
def test_removing_a_date_with_no_rows(acme, report):
    with pytest.raises(uploads.UploadProblems):
        uploads.remove_date(report, D("2026-03-31"), "ravi")


@pytest.mark.django_db
def test_calendar_shows_loaded_and_missing(acme, report):
    Dataset.objects.filter(pk=report.pk).update(how_often=Dataset.MONTH_END)
    confirm(acme, send(acme, report, FEB, as_of="2026-01-31")[0])
    confirm(acme, send(acme, report, FEB, as_of="2026-03-31")[0])
    page = acme.get(f"/findata/{report.id}/dates/?year=2026")
    assert page.status_code == 200
    assert D("2026-02-28") in page.context["missing_in_year"]
    assert page.context["loaded_in_year"] == 2
    text = page.content.decode()
    assert "31-03-2026 · 2 rows" in text
    assert "28-02-2026 · missing (Month-end)" in text
    assert acme.get(f"/findata/{report.id}/dates/?year=2025").context["loaded_in_year"] == 0


@pytest.mark.django_db
def test_upload_page_suggests_the_next_missing_date(acme, report):
    Dataset.objects.filter(pk=report.pk).update(how_often=Dataset.MONTH_END)
    confirm(acme, send(acme, report, FEB, as_of="2026-01-31")[0])
    page = acme.get(f"/findata/{report.id}/upload/")
    assert page.context["as_of"] == "2026-02-28"
    assert page.context["as_of_missing"] is True


@pytest.mark.django_db
def test_how_often_is_staff_only_and_keeps_undo(acme, report, settings):
    upload = send(acme, report, FEB, as_of="2026-02-28")[0]
    confirm(acme, upload)
    staff = settings.FINDATA
    settings.FINDATA = {**staff, "CAN_EDIT_DEFINITIONS": lambda request: False}
    response = acme.post(f"/findata/{report.id}/dates/how-often/", {"how_often": "month_end"})
    assert response.status_code == 403
    report.refresh_from_db()
    assert report.how_often == "any"

    settings.FINDATA = staff
    acme.post(f"/findata/{report.id}/dates/how-often/", {"how_often": "month_end"})
    report.refresh_from_db()
    assert report.how_often == "month_end"
    upload.refresh_from_db()
    assert uploads.undo_problems(upload) == []  # changing it touches no data


@pytest.mark.django_db
def test_other_company_cant_see_the_dates(client, report):
    session = client.session
    session["demo_company"] = "globex"
    session.save()
    assert client.get(f"/findata/{report.id}/dates/").status_code == 404


@pytest.mark.django_db
def test_periodic_reports_cant_be_linked_to(report):
    from findata import definition

    assert report.id not in [item["id"] for item in definition.link_targets("acme")]


@pytest.mark.django_db
def test_many_dates_preview_opens_on_per_date(acme, report):
    text = "Report date,Party,Amount\n31-01-2026,P1,10\n28-02-2026,P1,11\n"
    upload, _ = send(acme, report, text, map_as_of="Report date")
    page = acme.get(f"/findata/uploads/{upload.id}/")
    assert page.context["tab"] == "dates"
    assert [row["day"] for row in page.context["table"].page] == [D("2026-01-31"), D("2026-02-28")]


@pytest.mark.django_db
def test_last_upload_counts_a_removing_only_upload(acme, report):
    confirm(acme, send(acme, report, MARCH, as_of="2026-03-31")[0])
    upload, _ = send(acme, report, "Party,Amount\nP1,100\nP2,200\n", as_of="2026-03-31")
    Upload.objects.filter(pk=upload.pk).update(user="ravi")
    confirm(acme, upload)
    page = acme.get(f"/findata/{report.id}/dates/?year=2026").content.decode()
    assert "31-03-2026 · 2 rows · uploaded by ravi" in page


@pytest.mark.django_db
def test_upload_page_leaves_the_date_empty_when_a_column_had_it(acme, report):
    Dataset.objects.filter(pk=report.pk).update(remembered_mapping={"Report date": "_as_of_date"})
    page = acme.get(f"/findata/{report.id}/upload/")
    assert page.context["as_of"] == "" and page.context["date_column"] == "Report date"


@pytest.mark.django_db
def test_linking_to_a_periodic_report_says_why(report):
    from findata import definition

    payload = {
        "name": "Notes",
        "kind": "master",
        "description": "",
        "columns": [col("Code", is_key=True), col("Party", references=report.id)],
    }
    checked = definition.check("acme", payload, None)
    assert "periodic report can't be linked" in str(checked.row_errors)
