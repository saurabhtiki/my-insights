"""Pages and session behaviour: upload, isolation, clearing, limits."""

import time

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from my_insights.workspace import SESSION_KEY, store
from tests.conftest import csv_bytes, insights_settings

pytestmark = pytest.mark.django_db

HOME = reverse("my_insights:home")
UPLOAD = reverse("my_insights:upload")


def upload(client, *files, htmx=True):
    headers = {"HX-Request": "true"} if htmx else {}
    return client.post(UPLOAD, {"files": list(files)}, headers=headers)


def stock_file(name="stock.csv"):
    return SimpleUploadedFile(name, csv_bytes("Code,Qty", "A1,5", "A2,7"), "text/csv")


def workspace_of(client):
    return store.get(client.session.get(SESSION_KEY))


def test_home_page_shows_upload_form(client):
    response = client.get(HOME)
    assert response.status_code == 200
    assert b"Drop files here" in response.content
    assert b"No data yet" in response.content


def test_upload_shows_result_and_dataset(client):
    response = upload(client, stock_file())
    assert response.status_code == 200
    assert b"Loaded as stock (2 rows)" in response.content
    assert b'id="workspace-panel"' in response.content
    assert len(workspace_of(client).datasets) == 1


def test_upload_mix_of_good_and_bad_files(client):
    bad = SimpleUploadedFile("dupes.csv", b"A,a\n1,2\n")
    response = upload(client, stock_file(), bad)
    assert b"Loaded as stock" in response.content
    assert "“a” is used twice".encode() in response.content


def test_upload_without_files(client):
    response = upload(client)
    assert b"Choose at least one" in response.content


def test_plain_form_post_gets_full_page(client):
    response = upload(client, stock_file(), htmx=False)
    assert b"<html" in response.content


def test_upload_needs_csrf_token():
    client = Client(enforce_csrf_checks=True)
    response = upload(client, stock_file())
    assert response.status_code == 403


def test_uploads_never_use_temp_files(client, monkeypatch):
    """Files bigger than Django's 2.5 MB memory threshold would normally go to a temp file."""
    from django.core.files import uploadedfile

    def refuse(*args, **kwargs):
        raise AssertionError("an upload was written to a temp file")

    monkeypatch.setattr(uploadedfile.TemporaryUploadedFile, "__init__", refuse)
    big = SimpleUploadedFile("big.csv", b"A,B\n" + b"12345,abcdef\n" * 300_000)
    response = upload(client, big)
    assert b"Loaded as big (300,000 rows)" in response.content


@insights_settings(MAX_FILE_MB=1)
def test_too_big_file_is_skipped_and_named(client):
    big = SimpleUploadedFile("huge.csv", b"A\n" + b"1234567\n" * 200_000)
    response = upload(client, big, stock_file())
    assert b"huge.csv" in response.content
    assert b"larger than the 1 MB limit" in response.content
    assert b"Loaded as stock" in response.content


@insights_settings(MAX_FILES_PER_UPLOAD=1)
def test_too_many_files(client):
    response = upload(client, stock_file("one.csv"), stock_file("two.csv"))
    assert b"Loaded as one" in response.content
    assert b"Only 1 files can be uploaded at once" in response.content


@insights_settings(TOTAL_MEMORY_MB=0.0001)
def test_server_busy_refuses_uploads(client):
    upload(client, stock_file())  # first upload fits (memory was empty)
    response = upload(client, stock_file("more.csv"))
    assert b"server is busy" in response.content


def test_two_users_cannot_see_each_others_data():
    alice, bob = Client(), Client()
    upload(alice, stock_file("alice.csv"))
    assert b"alice" in alice.get(HOME).content
    assert b"alice" not in bob.get(HOME).content
    dataset_id = next(iter(workspace_of(alice).datasets))
    assert bob.get(reverse("my_insights:dataset", args=[dataset_id])).status_code == 404


def test_clear_button_wipes_data(client):
    upload(client, stock_file())
    workspace_id = client.session[SESSION_KEY]
    response = client.post(reverse("my_insights:clear"))
    assert response.status_code == 302
    assert store.get(workspace_id) is None


def test_idle_data_is_wiped_and_user_told(client):
    upload(client, stock_file())
    workspace_of(client).last_seen = time.monotonic() - 31 * 60
    response = client.get(HOME)
    assert len(store) == 0
    assert b"cleared after 30 minutes" in response.content


def test_keep_alive(client):
    upload(client, stock_file())
    workspace = workspace_of(client)
    workspace.last_seen = time.monotonic() - 26 * 60
    assert client.post(reverse("my_insights:keep_alive")).status_code == 204
    assert time.monotonic() - workspace.last_seen < 5
    client.post(reverse("my_insights:clear"))
    assert client.post(reverse("my_insights:keep_alive")).status_code == 410


def test_logout_wipes_data(client):
    user = User.objects.create_user("auditor", password="secret-pass-1")
    client.force_login(user)
    upload(client, stock_file())
    assert len(store) == 1
    from django.contrib.auth import logout
    from django.http import HttpRequest

    request = HttpRequest()
    request.session = client.session
    request.user = user
    logout(request)
    assert len(store) == 0


def test_dataset_profile_and_rows(client):
    upload(client, SimpleUploadedFile("p.csv", csv_bytes("page,Qty", "x,1", "y,N/A", "z,3")))
    dataset_id = next(iter(workspace_of(client).datasets))
    url = reverse("my_insights:dataset", args=[dataset_id])
    profile = client.get(url)
    assert b"Needs a look" in profile.content  # Qty has an odd value
    assert "“N/A” (row 3)".encode() in profile.content
    # A column called "page" must not break the table's own paging.
    data_rows = client.get(url, {"view": "rows", "page": "1"})
    assert data_rows.status_code == 200
    assert b"<td" in data_rows.content


def test_dataset_table_click_returns_only_table(client):
    upload(client, stock_file())
    dataset_id = next(iter(workspace_of(client).datasets))
    url = reverse("my_insights:dataset", args=[dataset_id])
    response = client.get(
        url, {"sort": "name"}, headers={"HX-Request": "true", "HX-Target": "insights-profile"}
    )
    assert b'id="insights-profile"' in response.content
    assert b"<html" not in response.content


def test_rename_dataset(client):
    upload(client, stock_file(), stock_file("other.csv"))
    dataset_id = next(iter(workspace_of(client).datasets))
    url = reverse("my_insights:dataset_rename", args=[dataset_id])
    assert client.post(url, {"name": "  Jan   stock "}).status_code == 302
    assert workspace_of(client).datasets[dataset_id].name == "Jan stock"
    taken = client.post(url, {"name": "OTHER"})
    assert taken.status_code == 400
    assert "already called “OTHER”".encode() in taken.content
    assert client.post(url, {"name": "  "}).status_code == 400


def test_remove_dataset(client):
    upload(client, stock_file())
    workspace = workspace_of(client)
    dataset_id = next(iter(workspace.datasets))
    table_name = workspace.datasets[dataset_id].table_name
    response = client.post(
        reverse("my_insights:dataset_remove", args=[dataset_id]), headers={"HX-Request": "true"}
    )
    assert b"No data yet" in response.content
    tables = workspace.connection.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    assert (table_name,) not in tables


def test_unknown_dataset_is_404(client):
    upload(client, stock_file())
    assert client.get(reverse("my_insights:dataset", args=[999])).status_code == 404


# ---------- fixes from the phase-1 code review ----------


def test_default_budget_ignores_other_programs(client, monkeypatch, settings):
    """With no TOTAL_MEMORY_MB, a nearly full server (other programs) must not block uploads."""
    import psutil

    settings.MY_INSIGHTS = {k: v for k, v in settings.MY_INSIGHTS.items() if k != "TOTAL_MEMORY_MB"}
    real = psutil.virtual_memory()
    monkeypatch.setattr(psutil, "virtual_memory", lambda: real._replace(percent=99.0))
    response = upload(client, stock_file())
    assert b"Loaded as stock" in response.content
    assert b"memory is" not in client.get(HOME).content  # no "memory is full" banner


def test_expired_dataset_link_explains_what_happened(client):
    upload(client, stock_file())
    dataset_id = next(iter(workspace_of(client).datasets))
    workspace_of(client).last_seen = time.monotonic() - 31 * 60
    response = client.get(reverse("my_insights:dataset", args=[dataset_id]), follow=True)
    assert b"cleared after 30 minutes" in response.content


def test_yes_no_column_filter_uses_yes_no(client):
    upload(client, SimpleUploadedFile("b.csv", csv_bytes("Name,Active", "a,yes", "b,no")))
    dataset_id = next(iter(workspace_of(client).datasets))
    url = reverse("my_insights:dataset", args=[dataset_id])
    response = client.get(url, {"view": "rows", "c1": "True"})  # ticked "Yes"
    cells = response.content.decode()
    assert 'truncate">a</td>' in cells
    assert 'truncate">b</td>' not in cells
    assert 'truncate">Yes</td>' in cells


def test_idle_warning_only_when_there_is_data(client):
    assert b"myInsightsIdle(" not in client.get(HOME).content
    response = upload(client, stock_file())
    assert b"myInsightsIdle(" in response.content  # arrives with the refreshed panel
