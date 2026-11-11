"""Phase 4.5: a saved query's answer used as a dataset (e.g. Top customers → their sales)."""

import pytest
from django.urls import reverse

from my_insights import linking
from my_insights.workspace import LinkSpec, LinkStep
from tests.conftest import insights_settings
from tests.test_querying import QUERY, post, upload

BY_ITEM = {
    "groups": "g1",
    "g1_column": "Item",
    "values": "v1",
    "v1_column": "Amount",
    "v1_agg": "sum",
}


def save_query(client, dataset, name="By item", **fields):
    post(client, dataset, action="save", name=name, **(fields or BY_ITEM))


def make_dataset(client, query_id):
    return client.post(reverse("my_insights:query_as_dataset", args=[query_id]))


@pytest.mark.django_db
def test_use_the_answer_as_a_dataset_and_query_it(client):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    page = client.get(QUERY + f"?open={query.query_id}").content.decode()
    assert "Use the answer as a dataset" in page

    response = make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    assert response.url == QUERY + f"?dataset={made.dataset_id}&made=1"
    assert made.name == "By item (result)" and made.query_id == query.query_id
    assert [c.name for c in made.columns] == ["Item", "Sum of Amount"]
    assert [c.kind for c in made.columns] == ["text", "integer"]
    assert made.row_count == 3 and made.columns[0].samples

    page = client.get(response.url).content.decode()
    assert "Made a dataset from this query" in page
    on_it = post(
        client, made, groups="g1", g1_column="Item", values="v1", v1_column="Sum of Amount"
    )
    assert "3 rows" in on_it.content.decode()

    again = make_dataset(client, query.query_id)  # one per query
    assert again.url == QUERY + f"?dataset={made.dataset_id}" and len(workspace.datasets) == 2
    home = client.get(reverse("my_insights:home")).content.decode()
    assert "From query By item" in home and "Query result" in home


@pytest.mark.django_db
def test_the_whole_answer_is_kept_not_only_the_preview(client):
    workspace, sales = upload(client)
    with insights_settings(PREVIEW_ROW_CAP=2):
        save_query(client, sales)
        query = next(iter(workspace.queries.values()))
        make_dataset(client, query.query_id)
    assert list(workspace.datasets.values())[-1].row_count == 3


@pytest.mark.django_db
def test_too_big_an_answer_is_refused_with_a_message(client):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    with insights_settings(MAX_ROWS=2):
        response = make_dataset(client, query.query_id)
    assert response.url == QUERY + f"?open={query.query_id}" and len(workspace.datasets) == 1
    page = client.get(response.url).content.decode()
    assert "into a dataset: the answer has more than 2 rows" in page
    assert "more than 2 rows" not in client.get(response.url).content.decode()  # shown once


@pytest.mark.django_db
def test_saving_the_query_updates_the_dataset(client):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    post(client, made, groups="g1", g1_column="Item", values="v1", v1_column="Sum of Amount")
    post(
        client,
        made,
        action="save",
        name="On items",
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Sum of Amount",
        v1_agg="sum",
    )

    by_branch = BY_ITEM | {"g1_column": "Branch", "query_id": query.query_id}
    save_query(client, sales, **by_branch)
    assert [c.name for c in made.columns] == ["Branch", "Sum of Amount"]
    assert made.row_count == 4
    on_items = next(q for q in workspace.queries.values() if q.name == "On items")
    page = client.get(QUERY + f"?open={on_items.query_id}").content.decode()
    assert "“Item” is no longer in By item (result)" in page


@pytest.mark.django_db
def test_a_query_cannot_ask_about_its_own_answer(client):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    page = client.get(QUERY + f"?open={query.query_id}").content.decode()
    assert ">By item (result)</option>" not in page

    looped = post(
        client,
        made,
        action="save",
        name="By item",
        query_id=query.query_id,
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Sum of Amount",
    )
    assert "is made from this query, so the query can" in looped.content.decode()


@pytest.mark.django_db
def test_removing_the_query_removes_its_dataset_and_what_is_built_on_it(client):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    post(
        client,
        made,
        action="save",
        name="On items",
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Sum of Amount",
    )
    home = client.get(reverse("my_insights:home")).content.decode()
    assert "The dataset “By item (result)” made from it goes too." in home
    assert "Saved queries removed too: “On items”." in home

    client.post(reverse("my_insights:query_remove", args=[query.query_id]))
    assert list(workspace.datasets) == [sales.dataset_id] and not workspace.queries
    tables = workspace.connection.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    assert (made.table_name,) not in tables

    # removing the uploaded dataset also removes query-datasets made on it
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    client.post(reverse("my_insights:dataset_remove", args=[sales.dataset_id]))
    assert not workspace.datasets and not workspace.queries


@pytest.mark.django_db
def test_across_is_kept_as_a_column_and_the_dataset_can_be_linked(client):
    workspace, sales = upload(client)
    fields = BY_ITEM | {"across": "a1", "a1_column": "Date", "a1_part": "month"}
    save_query(client, sales, **fields)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    assert [c.name for c in made.columns] == ["Item", "Date (month)", "Sum of Amount"]
    assert made.row_count == 5

    totals = BY_ITEM | {"query_id": query.query_id}
    save_query(client, sales, **totals)  # back to one row per item
    spec = LinkSpec(sales.dataset_id, [LinkStep(made.dataset_id, 0, [("Item", "Item")])])
    linked = linking.save_link(workspace, spec, "Sales with item totals")
    rows = workspace.connection.execute(
        f'SELECT "Item", "Sum of Amount" FROM {linked.table_name} ORDER BY 1'
    ).fetchall()
    assert rows[0] == ("A1", 300)


@pytest.mark.django_db
def test_another_session_cannot_use_my_query(client, django_user_model):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    other = type(client)()
    response = other.post(reverse("my_insights:query_as_dataset", args=[query.query_id]))
    assert response.status_code == 302 and len(workspace.datasets) == 1


@pytest.mark.django_db
def test_across_with_top_n_keeps_only_the_rows_shown(client):
    workspace, sales = upload(client)
    fields = BY_ITEM | {
        "across": "a1",
        "a1_column": "Date",
        "a1_part": "month",
        "sorts": "s1",
        "s1_target": "v1",
        "s1_desc": "1",
        "top_n": "1",
    }
    save_query(client, sales, name="Top item [by month]", **fields)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    rows = workspace.connection.execute(f"SELECT DISTINCT Item FROM {made.table_name}").fetchall()
    assert rows == [("A1",)]  # A1 (300) only, in its 2 months
    assert made.row_count == 2


@pytest.mark.django_db
def test_remove_confirm_with_a_question_mark_in_the_name(client):
    workspace, sales = upload(client)
    save_query(client, sales, name="Why low?")
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    from my_insights.views import query_remove_confirm

    text = query_remove_confirm(workspace, query)
    assert text == (
        "Remove the saved query “Why low?”? The dataset “Why low? (result)” made from it goes too."
    )


@pytest.mark.django_db
def test_a_linked_dataset_is_rebuilt_when_its_query_dataset_changes(client):
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    make_dataset(client, query.query_id)
    made = list(workspace.datasets.values())[-1]
    spec = LinkSpec(sales.dataset_id, [LinkStep(made.dataset_id, 0, [("Item", "Item")])])
    linked = linking.save_link(workspace, spec, "Sales with item totals")
    renamed = BY_ITEM | {"query_id": query.query_id, "v1_label": "Item total"}
    save_query(client, sales, **renamed)
    assert "Item total" in [c.name for c in linked.columns]
    assert "Sum of Amount" not in [c.name for c in linked.columns]
