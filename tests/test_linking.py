"""Phase 2: linking datasets (main + lookup lists → one combined dataset, saved as a view)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from my_insights import linking
from my_insights.linking import LinkRejected
from my_insights.loading import load_file
from my_insights.workspace import SESSION_KEY, LinkSpec, LinkStep, store
from tests.conftest import csv_bytes, insights_settings

SALES = csv_bytes(
    "Item Code,Cust,Qty,Name",
    "A1,c1,2,first",
    "a1,c2,3,second",
    "B2,c1,1,third",
    ",c3,5,no code",
    "Z9,c1,1,unknown item",
)
ITEMS = csv_bytes("Code,Name,Price,SubCat", "A1,Apple,10,S1", "B2,Ball,5,S2", "C3,Cup,7,S1")
CUSTOMERS = csv_bytes("Cust ID,City", "c1,Pune", "c2,Delhi")
SUBCATS = csv_bytes("Sub Cat,Category", "S1,Food", "S2,Toys")


def load(workspace, name, data):
    return load_file(workspace, name, data)[0]


@pytest.fixture
def shop(workspace):
    """Sales, Items, Customers and Sub-categories loaded into one workspace."""
    return (
        workspace,
        load(workspace, "sales.csv", SALES),
        load(workspace, "items.csv", ITEMS),
        load(workspace, "customers.csv", CUSTOMERS),
        load(workspace, "subcats.csv", SUBCATS),
    )


def sales_items(sales, items, join_type="left", ignore_case=True):
    step = LinkStep(items.dataset_id, 0, [("Item Code", "Code")], join_type)
    return LinkSpec(sales.dataset_id, [step], ignore_case=ignore_case)


def rows_of(workspace, dataset):
    return workspace.connection.execute(f"SELECT * FROM {dataset.table_name}").fetchall()


# ---------- join types ----------


def test_keep_all_main_rows(shop):
    workspace, sales, items, *_ = shop
    combined = linking.save_link(workspace, sales_items(sales, items), "Sales + Items")
    assert combined.row_count == 5
    assert [c.name for c in combined.columns] == [
        "Item Code", "Cust", "Qty", "Name", "Name (items)", "Price", "SubCat",
    ]  # fmt: skip
    rows = rows_of(workspace, combined)
    assert rows[0][4:6] == ("Apple", 10)
    assert rows[1][4] == "Apple"  # "a1" matches "A1": upper / lower case ignored
    assert rows[4][4] is None  # Z9 has no item: blank, but the sale stays
    price = next(c for c in combined.columns if c.name == "Price")
    assert price.kind == "integer" and price.blanks == 2


def test_only_matches(shop):
    workspace, sales, items, *_ = shop
    check = linking.check_link(
        workspace.connection, sales_items(sales, items, "inner"), workspace.datasets
    )
    assert check.ok and check.row_count == 3


def test_keep_everything_adds_unused_lookup_rows(shop):
    workspace, sales, items, *_ = shop
    combined = linking.save_link(workspace, sales_items(sales, items, "full"), "All")
    assert combined.row_count == 6  # 5 sales + item C3 that nobody bought
    cup = next(row for row in rows_of(workspace, combined) if row[4] == "Cup")
    assert cup[0] == "C3"  # the link column shows the lookup's value when the main one is blank


def test_case_sensitive_when_asked(shop):
    workspace, sales, items, *_ = shop
    check = linking.check_link(
        workspace.connection, sales_items(sales, items, ignore_case=False), workspace.datasets
    )
    assert check.steps[0].matched == 2  # "a1" no longer matches "A1"


# ---------- match counts and refusals ----------


def test_match_counts_and_blanks(shop):
    workspace, sales, items, *_ = shop
    step = linking.check_link(
        workspace.connection, sales_items(sales, items), workspace.datasets
    ).steps[0]
    assert (step.total, step.matched, step.blank) == (5, 3, 1)
    assert step.match_percent == 75  # 3 of the 4 sales that have an Item Code


def test_duplicate_lookup_value_is_refused(workspace):
    sales = load(workspace, "sales.csv", SALES)
    items = load(workspace, "items.csv", csv_bytes("Code,Price", "A1,50", "a1,55", "B2,5"))
    check = linking.check_link(workspace.connection, sales_items(sales, items), workspace.datasets)
    assert not check.ok
    assert "has 1 Code value listed more than once, e.g. A1 (2 times)" in check.problems[0]
    with pytest.raises(LinkRejected, match="must appear only once"):
        linking.save_link(workspace, sales_items(sales, items), "Bad")


def test_too_many_rows_is_refused(shop):
    workspace, sales, items, *_ = shop
    with insights_settings(MAX_ROWS=4):
        check = linking.check_link(
            workspace.connection, sales_items(sales, items), workspace.datasets
        )
    assert "more than the 4 limit" in check.problems[0]


def test_nothing_matching_with_only_matches_is_refused(workspace):
    sales = load(workspace, "sales.csv", SALES)
    items = load(workspace, "items.csv", csv_bytes("Code,Price", "X1,1"))
    check = linking.check_link(
        workspace.connection, sales_items(sales, items, "inner"), workspace.datasets
    )
    assert "No rows would be left" in check.problems[0]


def test_missing_column_and_missing_lookup(shop):
    workspace, sales, items, *_ = shop
    spec = LinkSpec(sales.dataset_id, [LinkStep(items.dataset_id, 0, [("Item Code", "Nope")])])
    assert (
        "has no column “Nope”"
        in linking.check_link(workspace.connection, spec, workspace.datasets).problems[0]
    )
    spec = LinkSpec(sales.dataset_id, [LinkStep(0)])
    assert (
        "choose a dataset"
        in linking.check_link(workspace.connection, spec, workspace.datasets).problems[0]
    )


def test_number_and_text_compared_as_text(workspace):
    sales = load(workspace, "sales.csv", csv_bytes("Code,Qty", "7,1", "007,2", "8,3"))
    items = load(workspace, "items.csv", csv_bytes("Code,Name", "007,Bond", "8,Eight"))
    # "007" keeps its zeros, so Items' Code is text; Sales' Code is mixed → text too here
    spec = LinkSpec(sales.dataset_id, [LinkStep(items.dataset_id, 0, [("Code", "Code")])])
    check = linking.check_link(workspace.connection, spec, workspace.datasets)
    assert check.ok and check.steps[0].matched == 2  # "007" and "8"; 7 ≠ "007"


def test_whole_number_key_against_text_key(workspace):
    sales = load(workspace, "sales.csv", csv_bytes("Code,Qty", "7,1", "8,2"))
    items = load(workspace, "items.csv", csv_bytes("Code,Name", "007,Bond", "8,Eight"))
    assert sales.columns[0].kind == "integer" and items.columns[0].kind == "text"
    spec = LinkSpec(sales.dataset_id, [LinkStep(items.dataset_id, 0, [("Code", "Code")])])
    check = linking.check_link(workspace.connection, spec, workspace.datasets)
    assert check.steps[0].matched == 1  # only 8 = "8"


def test_link_on_two_columns(workspace):
    sales = load(workspace, "sales.csv", csv_bytes("Item,Store,Qty", "A1,S1,1", "A1,S2,2"))
    prices = load(workspace, "prices.csv", csv_bytes("Item,Store,Price", "A1,S1,10", "A1,S2,12"))
    one_column = LinkSpec(sales.dataset_id, [LinkStep(prices.dataset_id, 0, [("Item", "Item")])])
    assert not linking.check_link(
        workspace.connection, one_column, workspace.datasets
    ).ok  # A1 twice
    both = LinkSpec(
        sales.dataset_id,
        [LinkStep(prices.dataset_id, 0, [("Item", "Item"), ("Store", "Store")])],
    )
    combined = linking.save_link(workspace, both, "Sales with prices")
    assert [row[-1] for row in rows_of(workspace, combined)] == [10, 12]
    assert [c.name for c in combined.columns] == ["Item", "Store", "Qty", "Price"]


# ---------- star, hierarchy, chains ----------


def test_star_and_hierarchy_in_one_dataset(shop):
    workspace, sales, items, customers, subcats = shop
    spec = LinkSpec(
        sales.dataset_id,
        [
            LinkStep(items.dataset_id, 0, [("Item Code", "Code")]),
            LinkStep(customers.dataset_id, 0, [("Cust", "Cust ID")]),
            LinkStep(subcats.dataset_id, 1, [("SubCat", "Sub Cat")]),  # linked to Items
        ],
    )
    combined = linking.save_link(workspace, spec, "Everything")
    names = [c.name for c in combined.columns]
    assert names[-3:] == ["SubCat", "City", "Category"]
    first = dict(zip(names, rows_of(workspace, combined)[0], strict=True))
    assert (first["Name (items)"], first["City"], first["Category"]) == ("Apple", "Pune", "Food")
    assert combined.row_count == 5
    assert combined.source_file == "sales, items, customers, subcats"


def test_view_uses_no_extra_memory(shop):
    workspace, sales, items, *_ = shop
    workspace.refresh_memory()
    before = workspace.memory_used_bytes
    linking.save_link(workspace, sales_items(sales, items), "Sales + Items")
    assert workspace.memory_used_bytes <= before + 256 * 1024


def test_chain_edit_and_cascade_remove(shop):
    workspace, sales, items, customers, _ = shop
    first = linking.save_link(workspace, sales_items(sales, items), "Sales + Items")
    chain_spec = LinkSpec(
        first.dataset_id, [LinkStep(customers.dataset_id, 0, [("Cust", "Cust ID")])]
    )
    chain = linking.save_link(workspace, chain_spec, "Sales + Items + Customers")
    assert "Price" in [c.name for c in chain.columns]

    # Edit the first one to "only matches": the chain is rebuilt too.
    linking.save_link(
        workspace, sales_items(sales, items, "inner"), "Sales + Items", first.dataset_id
    )
    assert workspace.datasets[first.dataset_id].row_count == 3
    assert workspace.datasets[chain.dataset_id].row_count == 3

    assert [d.name for d in workspace.dependants(items.dataset_id)] == [
        "Sales + Items",
        "Sales + Items + Customers",
    ]
    removed = workspace.remove_dataset(items.dataset_id)
    assert removed == ["Sales + Items + Customers", "Sales + Items", "items"]
    assert set(workspace.datasets) == {sales.dataset_id, customers.dataset_id, 4}


def test_edit_that_breaks_a_dependant_changes_nothing(shop):
    workspace, sales, items, customers, _ = shop
    first = linking.save_link(workspace, sales_items(sales, items), "Sales + Items")
    chain_spec = LinkSpec(
        first.dataset_id, [LinkStep(customers.dataset_id, 0, [("Price", "City")])]
    )
    chain = linking.save_link(workspace, chain_spec, "Odd chain")
    # Without Items, "Price" disappears from the first dataset, so the chain would break.
    only_customers = LinkSpec(
        sales.dataset_id, [LinkStep(customers.dataset_id, 0, [("Cust", "Cust ID")])]
    )
    with pytest.raises(LinkRejected, match="“Odd chain” is built from this dataset"):
        linking.save_link(workspace, only_customers, "Sales + Items", first.dataset_id)
    assert workspace.datasets[first.dataset_id].link.steps[0].lookup_id == items.dataset_id
    assert rows_of(workspace, workspace.datasets[chain.dataset_id])  # still works


def test_edit_cannot_use_itself(shop):
    workspace, sales, items, *_ = shop
    first = linking.save_link(workspace, sales_items(sales, items), "Sales + Items")
    loop = LinkSpec(first.dataset_id, [LinkStep(items.dataset_id, 0, [("Item Code", "Code")])])
    with pytest.raises(LinkRejected, match="can't be used here"):
        linking.save_link(workspace, loop, "Sales + Items", first.dataset_id)


# ---------- suggestions ----------


def test_suggestions_find_star_and_hierarchy(shop):
    workspace, sales, items, customers, subcats = shop
    found = {
        (s.main_id, s.lookup_id, s.main_column, s.lookup_column)
        for s in linking.suggest_links(workspace)
    }
    assert (sales.dataset_id, items.dataset_id, "Item Code", "Code") in found
    assert (sales.dataset_id, customers.dataset_id, "Cust", "Cust ID") in found
    assert (items.dataset_id, subcats.dataset_id, "SubCat", "Sub Cat") in found
    groups = linking.suggestion_groups(workspace)
    assert [g["main"].dataset_id for g in groups] == [sales.dataset_id]
    spec = groups[0]["spec"]
    sub_step = next(s for s in spec.steps if s.lookup_id == subcats.dataset_id)
    linked_to = spec.steps[sub_step.linked_to - 1]
    assert linked_to.lookup_id == items.dataset_id  # Sub-category hangs off Items


def test_low_overlap_is_not_suggested(workspace):
    load(workspace, "sales.csv", csv_bytes("Code,Qty", "A1,1", "A2,1", "A3,1", "A4,1"))
    load(workspace, "items.csv", csv_bytes("Code,Name", "A1,x", "Q1,y"))  # 25% match
    assert linking.suggest_links(workspace) == []


def test_names_look_alike():
    assert linking.names_look_alike("Item Code", "item_code")
    assert linking.names_look_alike("ItemCode", "Code")
    assert not linking.names_look_alike("Qty", "City")


# ---------- pages ----------

LINK = reverse("my_insights:link")


def shop_client(client):
    for name, data in (("sales.csv", SALES), ("items.csv", ITEMS)):
        client.post(
            reverse("my_insights:upload"),
            {"files": [SimpleUploadedFile(name, data, "text/csv")]},
            headers={"HX-Request": "true"},
        )
    return store.get(client.session[SESSION_KEY])


def form_data(sales, items, **extra):
    return {
        "main": sales.dataset_id,
        "previous_main": sales.dataset_id,
        "step_count": 1,
        "s0_lookup": items.dataset_id,
        "s0_previous_lookup": items.dataset_id,
        "s0_to": 0,
        "s0_previous_to": 0,
        "s0_join": "left",
        "s0_pairs": 1,
        "s0_p0_left": "Item Code",
        "s0_p0_right": "Code",
        "ignore_case": "on",
        **extra,
    }


@pytest.mark.django_db
def test_link_page_is_prefilled_from_suggestions(client):
    shop_client(client)
    response = client.get(LINK)
    page = response.content.decode()
    assert response.status_code == 200
    assert 'value="Item Code" selected' in page and 'value="Code" selected' in page
    assert "75% match" in page and "1 more row has a blank link value" in page
    assert "Preview: first 100 rows" in page


@pytest.mark.django_db
def test_link_create_and_open(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    response = client.post(
        LINK,
        form_data(sales, items, action="create", name="Sales + Items"),
        headers={"HX-Request": "true"},
    )
    combined = next(d for d in workspace.datasets.values() if d.link)
    assert response.headers["HX-Redirect"] == reverse(
        "my_insights:dataset", args=[combined.dataset_id]
    )
    page = client.get(response.headers["HX-Redirect"]).content.decode()
    assert "Built from sales, items" in page and "Edit link" in page
    home = client.get(reverse("my_insights:home")).content.decode()
    assert "Combined" in home and "This also removes “Sales + Items”" in home


@pytest.mark.django_db
def test_link_form_buttons(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    page = client.post(
        LINK, form_data(sales, items, action="add_pair:0"), headers={"HX-Request": "true"}
    )
    assert 'name="s0_pairs" value="2"' in page.content.decode()
    page = client.post(
        LINK, form_data(sales, items, action="add_step"), headers={"HX-Request": "true"}
    )
    assert "Lookup 2" in page.content.decode()
    page = client.post(
        LINK, form_data(sales, items, action="remove_step:0"), headers={"HX-Request": "true"}
    )
    assert "Add at least one lookup list" in page.content.decode()


@pytest.mark.django_db
def test_link_name_clash_is_refused(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    response = client.post(
        LINK, form_data(sales, items, action="create", name="sales"), headers={"HX-Request": "true"}
    )
    assert "Another dataset is already called “sales”" in response.content.decode()
    assert not any(d.link for d in workspace.datasets.values())


@pytest.mark.django_db
def test_link_preview_table_sorts(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    client.post(LINK, form_data(sales, items, draft="tab1"), headers={"HX-Request": "true"})
    response = client.get(
        reverse("my_insights:link_preview", args=["tab1"]) + "?sort=-c2",
        headers={"HX-Request": "true", "HX-Target": "insights-link-preview"},
    )
    assert response.status_code == 200 and "unknown item" in response.content.decode()


@pytest.mark.django_db
def test_suggestion_hints_on_home(client):
    shop_client(client)
    page = client.get(reverse("my_insights:link_suggestions")).content.decode()
    assert "can pick up details from" in page and "Item Code = Code" in page


@pytest.mark.django_db
def test_other_session_cannot_see_link_draft(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    client.post(LINK, form_data(sales, items, draft="tab1"), headers={"HX-Request": "true"})
    from django.test import Client

    stranger = Client().get(
        reverse("my_insights:link_preview", args=["tab1"]), headers={"HX-Request": "true"}
    )
    assert "unknown item" not in stranger.content.decode()


# ---------- review fixes ----------


def test_empty_link_column_asks_to_choose(shop):
    workspace, sales, items, *_ = shop
    spec = LinkSpec(sales.dataset_id, [LinkStep(items.dataset_id, 0, [("Item Code", "")])])
    problems = linking.check_link(workspace.connection, spec, workspace.datasets).problems
    assert problems == ["“items”: choose both link columns."]


def test_long_suggested_name_stays_under_limit(workspace):
    main = load(workspace, "sales.csv", SALES)
    lookups = [
        load(workspace, f"a very long lookup list name number {n}.csv", ITEMS) for n in range(4)
    ]
    spec = LinkSpec(main.dataset_id, [LinkStep(d.dataset_id) for d in lookups])
    name = linking.default_name(spec, workspace.datasets)
    assert len(name) <= 100 and name.endswith("(with 4 lookups)")


def test_hint_only_uses_its_own_main_dataset(shop):
    workspace = shop[0]
    load(workspace, "returns.csv", csv_bytes("Code,Qty", "A1,1", "B2,1", "A1,2"))
    for group in linking.suggestion_groups(workspace):
        lookups = [item["lookup"].dataset_id for item in group["items"]]
        assert len(lookups) == len(set(lookups))  # no lookup listed twice


@pytest.mark.django_db
def test_link_form_after_data_cleared_leaves_the_page(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    client.post(reverse("my_insights:clear"))
    response = client.post(LINK, form_data(sales, items), headers={"HX-Request": "true"})
    assert response.status_code == 204 and response.headers["HX-Redirect"]


@pytest.mark.django_db
def test_two_tabs_keep_their_own_preview(client):
    workspace = shop_client(client)
    sales, items = workspace.datasets.values()
    client.post(LINK, form_data(sales, items, draft="tab1"), headers={"HX-Request": "true"})
    client.post(
        LINK, form_data(sales, items, draft="tab2", s0_join="inner"), headers={"HX-Request": "true"}
    )
    headers = {"HX-Request": "true", "HX-Target": "insights-link-preview"}
    tab1 = client.get(reverse("my_insights:link_preview", args=["tab1"]), headers=headers)
    tab2 = client.get(reverse("my_insights:link_preview", args=["tab2"]), headers=headers)
    assert "unknown item" in tab1.content.decode()  # keep all main rows
    assert "unknown item" not in tab2.content.decode()  # only matches
