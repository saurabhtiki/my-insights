import io

import openpyxl
import pytest
from django.conf import settings
from django.test import override_settings

from my_insights.workspace import store


@pytest.fixture(autouse=True)
def empty_store():
    """Every test starts and ends with no workspaces in memory."""
    for workspace_id in list(store._workspaces):
        store.discard(workspace_id)
    yield
    for workspace_id in list(store._workspaces):
        store.discard(workspace_id)


@pytest.fixture
def workspace():
    _, workspace = store.create()
    return workspace


def csv_bytes(*lines: str, encoding: str = "utf-8") -> bytes:
    return ("\n".join(lines) + "\n").encode(encoding)


def excel_bytes(sheets: dict[str, list[list]]) -> bytes:
    """A workbook: {"Sheet name": [row, row, ...]} (a sheet with [] is left empty)."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def insights_settings(**changes):
    """override_settings for a few MY_INSIGHTS values, keeping the demo's other values."""
    return override_settings(MY_INSIGHTS={**settings.MY_INSIGHTS, **changes})
