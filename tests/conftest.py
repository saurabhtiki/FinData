import copy

import pytest
from django.db import connection

from demo.hooks import SESSION_KEY
from findata import definition
from findata.models import Dataset


def col(label, data_type="text", **rules):
    column = definition.blank_column()
    column.update(label=label, data_type=data_type, **rules)
    return column


CHART = {
    "name": "Chart of Accounts",
    "kind": "master",
    "description": "",
    "columns": [
        col("Account code", is_key=True),
        col("Account name", required=True),
        col("Group"),
        col("Opening balance", "decimal"),
    ],
}


def sales_payload(chart_id):
    return {
        "name": "Sales",
        "kind": "transaction",
        "description": "",
        "columns": [
            col("Voucher no", is_key=True),
            col("Line no", "integer", is_key=True),
            col("Voucher date", "date", required=True),
            col("Account code", references=chart_id),
            col("Amount", "decimal"),
        ],
    }


def define(company_key, payload, dataset=None) -> Dataset:
    """Check + save a definition; fails the test if the check finds problems."""
    checked = definition.check(company_key, copy.deepcopy(payload), dataset)
    assert checked.ok, (checked.form_errors, checked.row_errors)
    return definition.save(company_key, checked, dataset)


def sql(text, params=None):
    with connection.cursor() as cursor:
        cursor.execute(text, params)
        return cursor.fetchall() if cursor.description else []


@pytest.fixture
def acme(client):
    """A client working on company ACME."""
    session = client.session
    session[SESSION_KEY] = "acme"
    session.save()
    return client


@pytest.fixture
def chart(db):
    return define("acme", CHART)


@pytest.fixture
def sales(chart):
    return define("acme", sales_payload(chart.id))
