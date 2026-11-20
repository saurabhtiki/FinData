"""The real PostgreSQL tables: built from the definition, and changed safely later."""

import copy

import pytest
from django.db import IntegrityError, transaction

from findata import definition
from findata.schema_builder import SchemaBlocked, row_count

from .conftest import CHART, col, define, sales_payload, sql


def columns_of(schema, table):
    return {
        name: (data_type, nullable)
        for name, data_type, nullable in sql(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            [schema, table],
        )
    }


def constraint_types(schema, table):
    return sorted(
        row[0]
        for row in sql(
            "SELECT constraint_type FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND table_name = %s AND constraint_type <> 'CHECK'",
            [schema, table],
        )
    )


def edit(dataset, change):
    payload = definition.payload_of(dataset)
    change(payload)
    checked = definition.check(dataset.company_key, payload, dataset)
    assert checked.ok, (checked.form_errors, checked.row_errors)
    return definition.save(dataset.company_key, checked, dataset)


def add_chart_rows(*codes):
    for code in codes:
        sql(
            "INSERT INTO co_acme.chart_of_accounts (account_code, account_name) VALUES (%s, %s)",
            [code, f"Account {code}"],
        )


@pytest.mark.django_db
def test_tables_are_built_with_types_key_and_link(sales):
    chart_columns = columns_of("co_acme", "chart_of_accounts")
    assert chart_columns["account_code"] == ("text", "NO")
    assert chart_columns["account_name"] == ("text", "NO")
    assert chart_columns["group_name"] == ("text", "YES")
    assert chart_columns["opening_balance"] == ("numeric", "YES")
    assert {"_batch_id", "_updated_at"} <= set(chart_columns)

    sales_columns = columns_of("co_acme", "sales")
    assert sales_columns["line_no"] == ("bigint", "NO")
    assert sales_columns["voucher_date"] == ("date", "NO")
    assert constraint_types("co_acme", "sales") == ["FOREIGN KEY", "PRIMARY KEY"]


@pytest.mark.django_db
def test_link_and_key_are_enforced_by_the_database(sales):
    add_chart_rows("4010")
    insert = (
        "INSERT INTO co_acme.sales (voucher_no, line_no, voucher_date, account_code, amount) "
        "VALUES (%s, %s, '2026-11-20', %s, 100.50)"
    )
    sql(insert, ["S1", 1, "4010"])
    with pytest.raises(IntegrityError), transaction.atomic():
        sql(insert, ["S1", 2, "9999"])  # account not in chart
    with pytest.raises(IntegrityError), transaction.atomic():
        sql(insert, ["S1", 1, "4010"])  # same key twice
    assert row_count(sales) == 1


@pytest.mark.django_db
def test_periodic_report_key_includes_as_of_date(db):
    report = define(
        "acme",
        {
            "name": "Receivables",
            "kind": "snapshot",
            "columns": [col("Party", is_key=True), col("Due", "decimal")],
        },
    )
    insert = "INSERT INTO co_acme.receivables (_as_of_date, party, due) VALUES (%s, 'P1', 10)"
    sql(insert, ["2026-11-19"])
    sql(insert, ["2026-11-20"])  # same party, another date: fine
    with pytest.raises(IntegrityError), transaction.atomic():
        sql(insert, ["2026-11-20"])
    assert row_count(report) == 2


@pytest.mark.django_db
def test_companies_get_separate_schemas(chart):
    define("globex", CHART)
    assert row_count(chart) == 0
    assert columns_of("co_globex", "chart_of_accounts")


@pytest.mark.django_db
def test_empty_table_is_simply_rebuilt(chart):
    def change(payload):
        payload["name"] = "Ledgers"
        payload["columns"][2]["data_type"] = "integer"
        payload["columns"].append(col("GST no", required=True))

    edit(chart, change)
    assert columns_of("co_acme", "chart_of_accounts") == {}
    ledgers = columns_of("co_acme", "ledgers")
    assert ledgers["group_name"] == ("bigint", "YES")
    assert ledgers["gst_no"] == ("text", "NO")


@pytest.mark.django_db
def test_safe_changes_keep_the_rows(chart):
    add_chart_rows("1000", "2000")

    def change(payload):
        payload["name"] = "Ledgers"
        payload["columns"][1]["label"] = "Ledger name"
        payload["columns"][1]["name"] = "ledger_name"
        payload["columns"][2]["required"] = False
        payload["columns"].append(col("GST no"))
        del payload["columns"][3]  # opening balance

    ledgers = edit(chart, change)
    assert row_count(ledgers) == 2
    found = columns_of("co_acme", "ledgers")
    assert "ledger_name" in found and "gst_no" in found and "opening_balance" not in found
    assert sql("SELECT ledger_name FROM co_acme.ledgers ORDER BY 1")[0][0] == "Account 1000"


@pytest.mark.django_db
def test_type_change_is_checked_against_the_rows(chart):
    add_chart_rows("1000", "2000")
    sql("UPDATE co_acme.chart_of_accounts SET group_name = '12' WHERE account_code = '1000'")
    edit(chart, lambda payload: payload["columns"][2].update(data_type="integer"))
    assert columns_of("co_acme", "chart_of_accounts")["group_name"][0] == "bigint"

    sql("UPDATE co_acme.chart_of_accounts SET account_name = 'Cash' WHERE account_code = '1000'")
    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, lambda payload: payload["columns"][1].update(data_type="integer"))
    assert blocked.value.problems == [
        "“Account name”: 2 row(s) can't become Whole number, e.g. “Account 2000”."
    ]
    assert columns_of("co_acme", "chart_of_accounts")["account_name"][0] == "text"


@pytest.mark.django_db
def test_required_and_unique_are_checked_against_the_rows(chart):
    add_chart_rows("1000", "2000")
    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, lambda payload: payload["columns"][2].update(required=True))
    assert blocked.value.problems == ["“Group” is blank in 2 row(s), so it can't be required yet."]

    sql("UPDATE co_acme.chart_of_accounts SET account_name = 'Same'")
    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, lambda payload: payload["columns"][1].update(unique=True))
    assert blocked.value.problems == ["“Account name” can't be unique: “Same” appears 2 times."]


@pytest.mark.django_db
def test_new_required_column_on_a_table_with_rows_is_blocked(chart):
    add_chart_rows("1000")
    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, lambda payload: payload["columns"].append(col("GST no", required=True)))
    assert "is new and Required" in blocked.value.problems[0]
    assert "gst_no" not in columns_of("co_acme", "chart_of_accounts")


@pytest.mark.django_db
def test_key_change_with_data(chart):
    add_chart_rows("1000", "2000")
    sql("UPDATE co_acme.chart_of_accounts SET account_name = 'Same'")

    def move_key(payload):
        payload["columns"][0]["is_key"] = False
        payload["columns"][1]["is_key"] = True

    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, move_key)
    assert "The new key (Account name) is not unique" in blocked.value.problems[0]

    sql("UPDATE co_acme.chart_of_accounts SET account_name = account_code")
    edit(chart, move_key)
    with pytest.raises(IntegrityError), transaction.atomic():
        sql(
            "INSERT INTO co_acme.chart_of_accounts (account_code, account_name) VALUES ('3', '1000')"
        )


@pytest.mark.django_db
def test_key_cannot_change_while_others_link_to_it(sales):
    chart = sales.columns.get(name="account_code").references

    def change(payload):
        payload["columns"][0]["data_type"] = "integer"

    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, change)
    assert blocked.value.problems == [
        "Sales link(s) to this dataset's key, so the key can't change. Remove those links first."
    ]


@pytest.mark.django_db
def test_new_link_is_checked_against_the_rows(chart):
    sales = define("acme", sales_payload(chart.id))
    edit(sales, lambda payload: payload["columns"][3].update(references=None))
    add_chart_rows("4010")
    sql(
        "INSERT INTO co_acme.sales (voucher_no, line_no, voucher_date, account_code) "
        "VALUES ('S1', 1, '2026-11-20', '9999')"
    )
    with pytest.raises(SchemaBlocked) as blocked:
        edit(sales, lambda payload: payload["columns"][3].update(references=chart.id))
    assert blocked.value.problems == [
        (
            "“Account code”: 1 row(s) have values not in Chart of Accounts, e.g. “9999”. "
            "Add them to Chart of Accounts first."
        )
    ]


@pytest.mark.django_db
def test_kind_cannot_change_with_rows(chart):
    add_chart_rows("1000")
    with pytest.raises(SchemaBlocked):
        edit(chart, lambda payload: payload.update(kind="snapshot"))


@pytest.mark.django_db
def test_blocked_change_leaves_the_definition_as_it_was(chart):
    add_chart_rows("1000")
    payload = copy.deepcopy(definition.payload_of(chart))
    payload["name"] = "Renamed"
    payload["columns"][2]["required"] = True
    checked = definition.check("acme", payload, chart)
    with pytest.raises(SchemaBlocked):
        definition.save("acme", checked, chart)
    chart.refresh_from_db()
    assert chart.name == "Chart of Accounts"
    assert not chart.columns.get(name="group_name").required
    assert row_count(chart) == 1


@pytest.mark.django_db
def test_empty_linked_table_still_takes_a_new_required_column(sales):
    chart = sales.columns.get(name="account_code").references
    edit(chart, lambda payload: payload["columns"].append(col("GST no", required=True)))
    assert columns_of("co_acme", "chart_of_accounts")["gst_no"] == ("text", "NO")
    with pytest.raises(SchemaBlocked) as blocked:
        edit(chart, lambda payload: payload.update(kind="snapshot"))
    assert blocked.value.problems == [
        "The kind can't change while Sales link(s) to it. Keep it as it was."
    ]


@pytest.mark.django_db
def test_two_columns_can_swap_names_with_data(chart):
    add_chart_rows("1000")

    def swap(payload):
        name, group = payload["columns"][1], payload["columns"][2]
        name["name"], group["name"] = "group_name", "account_name"

    edit(chart, swap)
    assert sql("SELECT account_name, group_name FROM co_acme.chart_of_accounts") == [
        (None, "Account 1000")
    ]
