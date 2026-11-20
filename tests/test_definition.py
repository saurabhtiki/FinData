import copy

import pytest

from findata import definition, naming
from findata.models import Dataset

from .conftest import CHART, col, define


@pytest.mark.parametrize(
    "label, expected",
    [
        ("Ledger Name", "ledger_name"),
        ("GST No.", "gst_no"),
        ("2024 Sales", "c_2024_sales"),
        ("  Amount (₹) ", "amount"),
        ("Group", "group_name"),
        ("", ""),
    ],
)
def test_to_db_name(label, expected):
    assert naming.to_db_name(label) == expected


def test_name_problems_are_plain_words():
    assert naming.name_problem("order").startswith("“order” is a reserved word")
    assert "can't start with _" in naming.name_problem("_batch_id")
    assert naming.name_problem("Bad-Name")
    assert naming.name_problem("account_code") == ""


@pytest.mark.django_db
def test_check_fills_names_and_forces_key_required():
    checked = definition.check("acme", copy.deepcopy(CHART))
    assert checked.ok
    first = checked.payload["columns"][0]
    assert first["name"] == "account_code"
    assert first["required"] is True
    assert checked.payload["table_name"] == "chart_of_accounts"


@pytest.mark.django_db
def test_check_needs_a_key_and_names():
    payload = {"name": "", "kind": "nope", "columns": [col(""), col("Amount", "decimal")]}
    checked = definition.check("acme", payload)
    text = " ".join(checked.form_errors)
    assert "Give the dataset a name" in text
    assert "Pick what kind" in text
    assert "Tick Key" in text
    assert checked.row_errors[0] == ["Type a column name, e.g. Account code."]


@pytest.mark.django_db
def test_check_rejects_repeated_and_reserved_names():
    payload = copy.deepcopy(CHART)
    payload["columns"].append(col("Account Code"))
    payload["columns"].append(col("Order", name="order"))  # typed by hand
    checked = definition.check("acme", payload)
    assert "already used by row 1" in checked.row_errors[4][0]
    assert "reserved word" in checked.row_errors[5][0]


@pytest.mark.django_db
def test_check_rejects_bad_decimal_places():
    payload = copy.deepcopy(CHART)
    payload["columns"][3]["decimal_places"] = 9
    assert "Decimal places must be 0 to 6." in definition.check("acme", payload).row_errors[3]


@pytest.mark.django_db
def test_same_name_in_same_company_is_refused_but_other_company_is_fine(chart):
    assert "already has a dataset called" in definition.check("acme", CHART).form_errors[0]
    assert definition.check("globex", copy.deepcopy(CHART)).ok


@pytest.mark.django_db
def test_link_type_must_match_target_key(chart):
    payload = {
        "name": "Sales",
        "kind": "transaction",
        "columns": [col("Voucher no", is_key=True), col("Account", "integer", references=chart.id)],
    }
    errors = definition.check("acme", payload).row_errors[1]
    assert errors == ["Chart of Accounts's key (Account code) is Text; make this column Text too."]


@pytest.mark.django_db
def test_link_needs_a_one_column_key(chart):
    two_keys = define(
        "acme",
        {
            "name": "Stock",
            "kind": "master",
            "columns": [col("Item", is_key=True), col("Godown", is_key=True)],
        },
    )
    payload = {
        "name": "Moves",
        "kind": "transaction",
        "columns": [col("No", is_key=True), col("Item", references=two_keys.id)],
    }
    assert "needs a key of exactly one column" in definition.check("acme", payload).row_errors[1][0]


@pytest.mark.django_db
def test_cannot_link_to_another_companys_dataset(chart):
    payload = {
        "name": "Sales",
        "kind": "transaction",
        "columns": [col("No", is_key=True), col("Account", references=chart.id)],
    }
    errors = definition.check("globex", payload).row_errors[1]
    assert errors == ["The linked dataset no longer exists. Pick another one."]


@pytest.mark.django_db
def test_copy_keeps_links_only_when_this_company_has_the_target(sales):
    payload, notes = definition.copy_payload(sales, "globex")
    account = next(c for c in payload["columns"] if c["name"] == "account_code")
    assert account["references"] is None
    assert "doesn't have yet" in notes[0]
    assert all(c["id"] is None for c in payload["columns"])

    globex_chart = define("globex", CHART)
    payload, notes = definition.copy_payload(sales, "globex")
    account = next(c for c in payload["columns"] if c["name"] == "account_code")
    assert account["references"] == globex_chart.id
    assert notes == []
    assert define("globex", payload).name == "Sales"
    assert Dataset.objects.filter(company_key="globex").count() == 2
