"""A blank Excel template for a dataset: a Data sheet with the headers, and a Help sheet."""

import io

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

from .models import Dataset

HEADER_FILL = PatternFill("solid", fgColor="DCEFEA")
KEY_FILL = PatternFill("solid", fgColor="F9E2B8")


def build_template(dataset: Dataset) -> bytes:
    workbook = Workbook()
    data_sheet = workbook.active
    data_sheet.title = "Data"
    columns = list(dataset.columns.select_related("references"))
    for position, column in enumerate(columns, start=1):
        cell = data_sheet.cell(row=1, column=position, value=column.label)
        cell.font = Font(bold=True)
        cell.fill = KEY_FILL if column.is_key else HEADER_FILL
        cell.comment = Comment(_rules(column), "FinData")
        data_sheet.column_dimensions[cell.column_letter].width = max(14, len(column.label) + 4)
    data_sheet.freeze_panes = "A2"

    help_sheet = workbook.create_sheet("Help")
    headers = ["Column", "Database name", "Type", "Key", "Required", "Unique", "Links to", "Help"]
    help_sheet.append(headers)
    for cell in help_sheet[1]:
        cell.font = Font(bold=True)
    for column in columns:
        help_sheet.append(
            [
                column.label,
                column.name,
                column.type_label,
                "Yes" if column.is_key else "",
                "Yes" if column.required or column.is_key else "",
                "Yes" if column.unique else "",
                column.references.name if column.references else "",
                column.help_text,
            ]
        )
    help_sheet.append([])
    help_sheet.append(
        [f"{dataset.name} · {dataset.get_kind_display()}. Orange headers are the key."]
    )
    if dataset.is_snapshot:
        help_sheet.append(["Periodic report: you pick the As-of date when uploading."])
    for letter, width in zip("ABCDEFGH", (24, 22, 20, 8, 10, 8, 22, 40), strict=True):
        help_sheet.column_dimensions[letter].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _rules(column) -> str:
    rules = [column.type_label]
    if column.is_key:
        rules.append("Key: no blanks, no repeats")
    elif column.required:
        rules.append("Required")
    if column.unique:
        rules.append("Unique")
    if column.references_id:
        rules.append(f"Must exist in {column.references.name}")
    if column.help_text:
        rules.append(column.help_text)
    return "\n".join(rules)
