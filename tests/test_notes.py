"""Phase 7: KPI / note colours, note formatting and live values in notes."""

import json

import pytest

from my_insights import dashboard as dash
from my_insights.workspace import FilterItem, NoteValue
from tests.test_dashboard import board, draw  # noqa: F401  (board is a fixture)
from tests.test_dashboard_views import HTMX, add, setup, url  # noqa: F401  (setup is a fixture)

NOTE = 4  # the board fixture's note tile (index)


def shown(text, **values):
    """render_note with ready values, e.g. shown("[[Avg]]", Avg="₹ 5")."""
    return str(dash.render_note(text, {k: dash.ShownValue(v, name="Q") for k, v in values.items()}))


# ---------- formatting ----------


def test_marks_become_styles():
    html = shown("**bold** _italic_ __under__")
    assert html == "<div><strong>bold</strong> <em>italic</em> <u>under</u></div>"
    assert shown("# Big") == '<div class="text-xl font-bold">Big</div>'
    assert shown("## Large") == '<div class="text-base font-semibold">Large</div>'
    assert shown("-# small") == '<div class="text-xs">small</div>'
    assert shown("- a\n- b") == '<ul class="list-disc pl-5"><li>a</li><li>b</li></ul>'
    assert shown("1. a\n2. b") == '<ol class="list-decimal pl-5"><li>a</li><li>b</li></ol>'
    # a list ends at a plain line; bullets then numbers are two lists
    assert shown("- a\n1. b\nc").count("</ul><ol") == 1 and shown("- a\nc").endswith("<div>c</div>")
    # a numbered list keeps its first number
    assert shown("5. e\n6. f").startswith('<ol start="5" class="list-decimal pl-5">')


def test_a_value_right_after_a_link_stays_out_of_it():
    html = shown("see https://x.com/r[[T]]", T="450")
    assert '<a href="https://x.com/r" rel="nofollow">https://x.com/r</a> <span' in html
    assert html.endswith(">450</span></div>")


def test_plain_old_notes_look_as_before():
    assert shown("Before GST\n\nsee https://x.com/a_b_c") == (
        '<div>Before GST<br><br>see <a href="https://x.com/a_b_c" rel="nofollow">'
        "https://x.com/a_b_c</a></div>"
    )
    assert shown("snake_case_name and 2*3*4") == "<div>snake_case_name and 2*3*4</div>"


def test_nothing_typed_can_run():
    html = shown('<script>alert(1)</script> **<b onclick="x">hi</b>** [[<img>]]')
    assert "<script>" not in html and "<b " not in html and "<img>" not in html
    assert "&lt;script&gt;" in html and "<strong>&lt;b onclick=" in html
    assert shown("[[A]]", A='<i>"x"</i>').count("&lt;i&gt;") == 1


# ---------- live values ----------


def test_values_show_their_number_or_why_not():
    assert "Avg is " in shown("Avg is [[Avg]]", Avg="₹ 1,245.50")
    assert ">₹ 1,245.50</span>" in shown("Avg is [[ Avg ]]", Avg="₹ 1,245.50")
    assert "⚠ unknown value [[Other]]" in shown("[[Other]]", Avg="1")
    gone = {"A": dash.ShownValue(problem="This query was removed.", gone=True)}
    assert "⚠ value removed" in dash.render_note("x [[A]] y", gone)
    preview = str(dash.render_note("**[[A]]**", {"A": None}))  # the editor: a name chip
    assert (
        preview == '<div><strong><span class="badge badge-sm badge-outline">A</span></strong></div>'
    )


def test_note_values_follow_filters_and_clicks(board):  # noqa: F811
    workspace, board = board
    note = board.tiles[NOTE]
    note.text = "Sales [[Total]], avg [[Avg]], names [[Gone]]"
    note.values = [
        NoteValue(1, "amt", "Total"),
        NoteValue(1, "avg", "Avg"),
        NoteValue(1, "nope", "Gone"),
    ]
    html = draw(workspace, board, NOTE).note_html
    assert ">₹ 450</span>" in html and ">90.00</span>" in html and "⚠ value removed" in html

    board.filters = [FilterItem("f1", "Branch", "", "in", ["MUM"])]
    assert ">₹ 300</span>" in draw(workspace, board, NOTE).note_html

    board.filters = []
    click = dash.Click(1, "Branch", "", "text", "DEL", "Branch = DEL")
    assert ">₹ 50</span>" in draw(workspace, board, NOTE, [click]).note_html

    # a value from data without the field: counted in full, with the badge
    note.values = [NoteValue(1, "amt", "Total")]
    workspace.queries[1].spec.dataset_id = workspace.queries[3].spec.dataset_id  # Items
    view = draw(workspace, board, NOTE, [click])
    assert view.skipped == ["Not filtered by Branch (no such field)"]


def test_a_note_value_brings_its_fields_to_the_filter_bar(board):  # noqa: F811
    workspace, board = board
    board.tiles = [board.tiles[NOTE]]  # only the note is left
    assert dash.dashboard_fields(workspace, board) == {}
    board.tiles[0].values = [NoteValue(1, "amt", "Total")]
    assert "Branch" in dash.dashboard_fields(workspace, board)
    board.tiles[0].text = "[[Total]]"
    board.filters = [FilterItem("f1", "Branch", "", "in", ["MUM"])]
    assert ">₹ 300</span>" in draw(workspace, board, 0).note_html


def test_a_removed_query_only_breaks_its_own_spot(board):  # noqa: F811
    workspace, board = board
    note = board.tiles[NOTE]
    note.text = "A [[Total]] B"
    note.values = [NoteValue(99, "amt", "Total")]
    html = draw(workspace, board, NOTE).note_html
    assert html.startswith("<div>A ") and "⚠ value removed" in html and html.endswith(" B</div>")


# ---------- colours ----------


def test_colour_classes():
    tile = dash.DashboardTile(1, "kpi", color="green")
    assert dash.tile_color_class(tile) == "bg-green-500/10 border-green-500/40"
    tile.tone = "strong"
    assert dash.tile_color_class(tile) == "bg-green-600 text-white border-green-600"
    assert dash.is_strong(tile)
    assert dash.tile_color_class(dash.DashboardTile(1, "kpi", color="pink")) == "bg-base-100"
    assert dash.tile_color_class(dash.DashboardTile(1, "chart", color="red")) == "bg-base-100"


# ---------- pages ----------


def layout(client, dashboard, data):
    return client.post(url("dashboard_layout", dashboard.dashboard_id), data, headers=HTMX)


@pytest.mark.django_db
def test_colour_and_values_are_saved_and_shown(setup, client):  # noqa: F811
    _, query, dashboard = setup
    value_key = query.spec.values[0].key
    add(client, dashboard, kind="kpi", kpi_value=f"{query.query_id}:{value_key}")
    add(client, dashboard, kind="note", text="first")
    ref = f"{query.query_id}:{value_key}"
    values = [
        {"ref": ref, "label": "Total  [sales]"},  # spaces / brackets cleaned
        {"ref": ref, "label": "Total sales"},  # same name again: dropped
        {"ref": "999:x", "label": "Lost"},  # no such query: dropped
        {"ref": ref, "label": ""},  # no name: dropped
    ]
    layout(
        client,
        dashboard,
        {
            "order": ["1", "2"],
            "t1_color": "green",
            "t1_tone": "strong",
            "t2_color": "pink",  # not a colour: plain
            "t2_tone": "loud",
            "t2_text": "**Sales** [[Total sales]]",
            "t2_values": json.dumps(values),
        },
    )
    kpi, note = dashboard.tiles
    assert (kpi.color, kpi.tone) == ("green", "strong")
    assert (note.color, note.tone) == ("", "soft")
    assert [(v.query_id, v.value_key, v.label) for v in note.values] == [
        (query.query_id, value_key, "Total sales")
    ]

    page = client.get(url("dashboard", dashboard.dashboard_id) + "?edit=1").content.decode()
    assert "bg-green-600 text-white" in page and 'name="t1_color"' in page
    assert "noteEditor(" in page and url("note_preview", dashboard.dashboard_id, 2) in page
    assert "＋ Insert value" in page
    # the note reloads when a filter on its value's field changes
    assert '"Item"' in page.split('data-tile="2"')[1].split(">")[0].replace("&quot;", '"')

    kpi_html = client.get(url("dashboard_tile", dashboard.dashboard_id, 1), headers=HTMX)
    assert "text-primary" not in kpi_html.content.decode().split("stat-value")[1][:80]
    note_html = client.get(url("dashboard_tile", dashboard.dashboard_id, 2), headers=HTMX)
    assert "<strong>Sales</strong>" in note_html.content.decode()
    assert ">450</span>" in note_html.content.decode()

    # bad JSON keeps nothing (logged), never an error page
    response = layout(client, dashboard, {"order": ["1", "2"], "t2_values": "{oops"})
    assert response.status_code == 200 and dashboard.tiles[1].values == []


@pytest.mark.django_db
def test_at_most_ten_values(setup, client):  # noqa: F811
    _, query, dashboard = setup
    add(client, dashboard, kind="note", text="x")
    ref = f"{query.query_id}:{query.spec.values[0].key}"
    values = [{"ref": ref, "label": f"V{number}"} for number in range(12)]
    layout(client, dashboard, {"order": ["1"], "t1_values": json.dumps(values)})
    assert len(dashboard.tiles[0].values) == dash.MAX_NOTE_VALUES


@pytest.mark.django_db
def test_note_preview_shows_marks_and_value_chips(setup, client):  # noqa: F811
    _, _, dashboard = setup
    add(client, dashboard, kind="note", text="x")
    response = client.post(
        url("note_preview", dashboard.dashboard_id, 1),
        {"t1_text": "# Hi [[A]] [[B]]", "t1_values": json.dumps([{"ref": "1:a", "label": "A"}])},
        headers=HTMX,
    )
    html = response.content.decode()
    assert html.startswith('<div class="text-xl font-bold">Hi <span class="badge')
    assert "⚠ unknown value [[B]]" in html
    empty = client.post(
        url("note_preview", dashboard.dashboard_id, 1), {"t1_text": ""}, headers=HTMX
    )
    assert "Nothing typed yet" in empty.content.decode()


@pytest.mark.django_db
def test_html_file_has_colours_formatting_and_values(setup, client):  # noqa: F811
    _, query, dashboard = setup
    ref = f"{query.query_id}:{query.spec.values[0].key}"
    add(client, dashboard, kind="note", text="x")
    layout(
        client,
        dashboard,
        {
            "order": ["1"],
            "t1_color": "red",
            "t1_tone": "soft",
            "t1_text": "__Total__ [[T]]",
            "t1_values": json.dumps([{"ref": ref, "label": "T"}]),
        },
    )
    html = client.post(url("dashboard_export", dashboard.dashboard_id)).content.decode()
    assert "bg-red-500/10 border-red-500/40" in html
    assert "<u>Total</u>" in html and ">450</span>" in html


@pytest.mark.django_db
def test_add_tile_box_sets_colour_formatting_and_values(setup, client):  # noqa: F811
    _, query, dashboard = setup
    ref = f"{query.query_id}:{query.spec.values[0].key}"
    page = client.get(url("dashboard", dashboard.dashboard_id) + "?edit=1").content.decode()
    box = page.split('id="add-tile-dialog"')[1]
    assert 'name="t0_text"' in box and "＋ Insert value" in box and 'name="t0_color"' in box
    add(
        client,
        dashboard,
        kind="note",
        t0_text="**Sales** [[T]]",
        t0_values=json.dumps([{"ref": ref, "label": "T"}]),
        t0_color="teal",
        t0_tone="strong",
    )
    add(client, dashboard, kind="kpi", kpi_value=ref, t0_color="red")
    note, kpi = dashboard.tiles
    assert (note.color, note.tone, note.text) == ("teal", "strong", "**Sales** [[T]]")
    assert [value.label for value in note.values] == ["T"]
    assert (kpi.color, kpi.tone) == ("red", "soft")
    html = client.get(url("dashboard_tile", dashboard.dashboard_id, 1), headers=HTMX)
    assert (
        "<strong>Sales</strong>" in html.content.decode() and ">450</span>" in html.content.decode()
    )
