"""Live Postgres / MySQL ("From database"): the form, safety checks, listing, import with an own
query, foreign keys, Refresh and friendly errors. A small fake server (SQLite underneath)
stands in for the real one; tests/test_db_live.py runs the same against real servers."""

import datetime
import decimal
import logging
import sqlite3

import pytest
from django.urls import reverse

from my_insights import db_connect
from my_insights.db_connect import check_select, server_from_form
from my_insights.db_loading import SqliteReader, cell_text, load_tables, refresh_from_server
from my_insights.linking import suggest_links
from my_insights.loading import UploadRejected
from my_insights.workspace import SESSION_KEY, DbServer, DbTable, store
from tests.conftest import insights_settings

pytestmark = pytest.mark.django_db

PASSWORD = "s3cret-pass"
ERP = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, city TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                     amount REAL, ordered_on TEXT);
INSERT INTO customers VALUES (1, 'Pune'), (2, 'Mumbai');
INSERT INTO orders VALUES (10, 1, 500, '2026-04-01'), (11, 2, 300, '2026-04-02'),
                          (12, 1, 950, '2026-04-03');
"""


class FakeServer:
    """Acts like db_connect.Postgres, but each connection is a fresh SQLite database built from
    `script` (change the script to change the "server's" data)."""

    kind, title, extra, default_port = "postgres", "PostgreSQL", "postgres", 5432
    errors = (sqlite3.Error,)

    def __init__(self):
        self.script = ERP

    def load(self):
        pass

    def connect(self, server):
        if server.password != PASSWORD:
            raise sqlite3.OperationalError("password authentication failed for user")
        connection = sqlite3.connect(":memory:")
        connection.executescript(self.script)
        connection.execute("PRAGMA query_only = ON")
        return connection

    def stream(self, connection, select):
        return connection.execute(select)

    def tables(self, connection, server):
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        tables = []
        for (name,) in names:
            select = f'SELECT * FROM "{name}"'
            columns = [c[0] for c in connection.execute(f"{select} LIMIT 0").description]
            rows = connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            tables.append(DbTable(name, "table", rows, columns, select, estimated=True))
        return tables

    def foreign_keys(self, connection, server):
        names = [t.name for t in self.tables(connection, server)]
        return SqliteReader(connection).foreign_keys(names)

    def message(self, error, server):
        if "password" in str(error):
            return "The user name or password is wrong."
        return f"The database said: {error}"


@pytest.fixture
def fake(monkeypatch):
    server = FakeServer()
    monkeypatch.setitem(db_connect.DIALECTS, "postgres", server)
    return server


FORM = {
    "kind": "postgres",
    "host": "192.168.1.20",
    "port": "",
    "database": "erp",
    "user": "report",
    "password": PASSWORD,
}


def post(client, name, data=None, **kwargs):
    return client.post(
        reverse(f"my_insights:{name}", kwargs=kwargs), data or {}, headers={"HX-Request": "true"}
    ).content.decode()


def connected(client):
    text = post(client, "db_connect", FORM)
    workspace = store.get(client.session.get(SESSION_KEY))
    return text, workspace, next(iter(workspace.pending_databases))


# ---------- the form and safety checks ----------


def test_form_fills_the_usual_port_and_hides_the_password():
    server = server_from_form({**FORM, "kind": "mysql"})
    assert server.port == 3306 and server.label == "erp on 192.168.1.20"
    assert PASSWORD not in repr(server)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"host": ""}, "host name or IP"),
        ({"host": "erp local"}, "host name or IP"),
        ({"host": "/var/run/postgresql"}, "host name or IP"),
        ({"host": "erp.local,localhost"}, "host name or IP"),
        ({"password": ""}, "password"),
        ({"port": "99999"}, "port must be a number"),
        ({"port": "abc"}, "port must be a number"),
        ({"database": " "}, "database name"),
        ({"user": ""}, "database user"),
        ({"kind": "oracle"}, "PostgreSQL or MySQL"),
    ],
)
def test_form_mistakes(change, message):
    with pytest.raises(UploadRejected, match=message):
        server_from_form({**FORM, **change})


def test_switched_off(fake):
    with insights_settings(ALLOW_DB_CONNECT=False), pytest.raises(UploadRejected, match="off"):
        db_connect.list_tables(server_from_form(FORM))


def test_only_allowed_hosts(fake):
    server = server_from_form(FORM)
    with (
        insights_settings(DB_ALLOWED_HOSTS=["erp.local"]),
        pytest.raises(UploadRejected, match="DB_ALLOWED_HOSTS"),
    ):
        db_connect.list_tables(server)
    with insights_settings(DB_ALLOWED_HOSTS=["192.168.1.20"]):
        assert db_connect.list_tables(server)


def test_driver_missing(monkeypatch):
    def missing(self):
        raise ImportError("No module named 'psycopg'")

    monkeypatch.setattr(db_connect.Postgres, "load", missing)
    with pytest.raises(UploadRejected, match=r'uv add "my-insights\[postgres\]"'):
        db_connect.list_tables(server_from_form(FORM))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "  select 1;  ",
        "WITH big AS (SELECT * FROM orders) SELECT * FROM big",
        "-- this year\nSELECT * FROM orders",
        "/* note */ (SELECT 1)",
    ],
)
def test_select_is_allowed(sql):
    assert check_select(sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("", "Write a query"),
        ("DELETE FROM orders", "Only a SELECT"),
        ("-- SELECT\nDROP TABLE orders", "Only a SELECT"),
        ("SELECT 1; DROP TABLE orders", "one query only"),
    ],
)
def test_other_statements_are_refused(sql, message):
    with pytest.raises(UploadRejected, match=message):
        check_select(sql)


# ---------- connect, import, refresh ----------


def test_connect_lists_tables_unticked_with_estimates(client, fake):
    text, workspace, _ = connected(client)
    assert "Found 2 tables you may read" in text
    assert "about 3 rows" in text and "Own query" in text
    assert 'value="orders"  class' in text  # not ticked: a server may have hundreds of tables
    assert workspace.datasets == {}


def test_wrong_password_is_a_message_and_is_never_logged(client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    text = post(client, "db_connect", {**FORM, "password": "wrong-one"})
    assert "Not connected" in text and "user name or password is wrong" in text
    post(client, "db_connect", FORM)
    assert "wrong-one" not in caplog.text and PASSWORD not in caplog.text


def test_import_tables_and_own_query(client, fake):
    _, workspace, token = connected(client)
    text = post(
        client,
        "db_import",
        {
            "tables": ["orders", "customers"],
            "query_name": "pune_orders",
            "query_sql": "SELECT o.* FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE c.city = 'Pune'",
        },
        token=token,
    )
    assert (
        "Loaded as erp – customers (2 rows), erp – orders (3 rows), pune_orders (2 rows)." in text
    )
    assert "Database" in text and "Refresh" in text
    customers, orders, pune = workspace.datasets.values()
    assert orders.source_file == "erp on 192.168.1.20 › orders"
    assert {c.name: c.kind for c in orders.columns}["ordered_on"] == "date"
    assert pune.db_source.select.startswith("SELECT o.*")
    assert orders.db_source.server is customers.db_source.server
    assert workspace.pending_databases == {}
    with workspace.lock:
        first = suggest_links(workspace)[0]
    assert (first.main_id, first.lookup_id, first.from_database) == (
        orders.dataset_id,
        customers.dataset_id,
        True,
    )


def test_only_a_query(client, fake):
    _, workspace, token = connected(client)
    post(client, "db_import", {"query_sql": "SELECT city FROM customers"}, token=token)
    [dataset] = workspace.datasets.values()
    assert dataset.name == "erp – query"


def test_nothing_ticked_and_no_query(client, fake):
    _, workspace, token = connected(client)
    text = post(client, "db_import", {}, token=token)
    assert "Tick at least one table, or write a query, to import." in text
    assert token in workspace.pending_databases


def test_a_bad_query_keeps_the_pick_list_and_loads_nothing(client, fake):
    _, workspace, token = connected(client)
    text = post(
        client, "db_import", {"tables": ["orders"], "query_sql": "DELETE FROM orders"}, token=token
    )
    assert "Only a SELECT query" in text
    text = post(
        client,
        "db_import",
        {"tables": ["orders"], "query_sql": "SELECT nope FROM orders"},
        token=token,
    )
    assert "Query “erp – query”: The database said: no such column: nope" in text
    assert workspace.datasets == {} and token in workspace.pending_databases


def test_row_limit(client, fake):
    _, workspace, token = connected(client)
    with insights_settings(MAX_ROWS=2):
        text = post(client, "db_import", {"tables": ["orders"]}, token=token)
    assert "Table “orders”: It has more than 2 data rows (the limit). Use an Own query" in text
    assert workspace.datasets == {}


def test_refresh_reads_again_and_keeps_the_dataset(client, fake):
    _, workspace, token = connected(client)
    post(client, "db_import", {"tables": ["orders"]}, token=token)
    [orders] = workspace.datasets.values()
    fake.script += "INSERT INTO orders VALUES (13, 2, 120, '2026-04-04');"
    text = post(client, "dataset_refresh", dataset_id=orders.dataset_id)
    assert "Refreshed" in text and "erp – orders (4 rows)" in text
    assert list(workspace.datasets) == [orders.dataset_id] and orders.row_count == 4
    count = workspace.connection.execute(f"SELECT count(*) FROM {orders.table_name}").fetchall()
    assert count == [(4,)]


def test_refresh_failure_keeps_the_old_data(client, fake):
    _, workspace, token = connected(client)
    post(client, "db_import", {"tables": ["orders"]}, token=token)
    [orders] = workspace.datasets.values()
    orders.db_source.server.password = "changed"
    text = post(client, "dataset_refresh", dataset_id=orders.dataset_id)
    assert "Not refreshed" in text and "The earlier data is kept." in text
    assert orders.row_count == 3 and len(workspace.datasets) == 1


def test_refresh_notes_a_column_that_is_gone(workspace, fake):
    server = server_from_form(FORM)
    pending = workspace.add_pending(server.label, None, db_connect.list_tables(server), server)
    [orders] = load_tables(workspace, pending.token, ["orders"])
    fake.script = (
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount REAL);"
        "INSERT INTO orders VALUES (10, 1, 500);"
    )
    [note] = refresh_from_server(workspace, orders)
    assert "no longer has: ordered_on" in note


# ---------- values and messages ----------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (True, "true"),
        (decimal.Decimal("500.50"), "500.50"),
        (datetime.datetime(2026, 4, 3, 10, 30), "2026-04-03 10:30:00"),
        (datetime.datetime(2026, 4, 3, 10, 30, 5, 123456), "2026-04-03 10:30:05"),
        (datetime.datetime(2026, 4, 3), "2026-04-03"),
        (
            datetime.datetime(
                2026, 4, 3, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=5))
            ),
            "2026-04-03 10:30:00",
        ),
        ({"city": "Pune"}, '{"city": "Pune"}'),
        (memoryview(b"\x01\x02"), "(binary, 2 bytes)"),
    ],
)
def test_cell_text(value, text):
    assert cell_text(value) == text


SERVER = DbServer("postgres", "erp.local", 5432, "erp", "report", PASSWORD)


@pytest.mark.parametrize(
    ("error_name", "text", "message"),
    [
        ("QueryCanceled", "canceling statement due to statement timeout", "ran longer than"),
        ("ReadOnlySqlTransaction", "cannot execute INSERT", "Only reading is allowed"),
        ("InsufficientPrivilege", "permission denied for table x", "may not read this"),
        (
            "OperationalError",
            'password authentication failed for user "report"',
            "password is wrong",
        ),
        ("OperationalError", 'database "erp" does not exist', "no database “erp”"),
        ("OperationalError", "could not translate host name", "could not be found"),
        ("OperationalError", "Connection refused", "Nothing answered at erp.local, port 5432"),
        ("OperationalError", "timeout expired", "did not answer within"),
        ("OperationalError", "no pg_hba.conf entry for host", "pg_hba.conf"),
        ("SyntaxError", 'syntax error at or near "FORM"', "The database said: syntax error"),
    ],
)
def test_postgres_messages(error_name, text, message):
    import psycopg

    dialect = db_connect.Postgres()
    dialect.load()
    error_class = getattr(psycopg.errors, error_name, None) or getattr(psycopg, error_name)
    assert message in dialect.message(error_class(text), SERVER)


@pytest.mark.parametrize(
    ("code", "text", "message"),
    [
        (1045, "Access denied for user", "password is wrong"),
        (1049, "Unknown database 'erp'", "no database “erp”"),
        (1792, "Cannot execute statement in a READ ONLY transaction.", "Only reading"),
        (3024, "Query execution was interrupted", "ran longer than"),
        (
            2003,
            "Can't connect to MySQL server on 'erp.local' ([Errno 11001] getaddrinfo failed)",
            "could not be found",
        ),
        (
            2003,
            "Can't connect ... [WinError 10061] No connection could be made because the target machine actively refused it",
            "Nothing answered",
        ),
        (1146, "Table 'erp.nope' doesn't exist", "The database said: Table 'erp.nope'"),
    ],
)
def test_mysql_messages(code, text, message):
    import pymysql

    dialect = db_connect.MySql()
    dialect.load()
    assert message in dialect.message(pymysql.err.OperationalError(code, text), SERVER)


def test_another_user_on_the_same_database_gets_its_own_pick_list(client, fake):
    connected(client)
    post(client, "db_connect", {**FORM, "user": "audit"})
    _, workspace, _ = connected(client)  # the first user again: replaces its own list only
    users = sorted(p.server.user for p in workspace.pending_databases.values())
    assert users == ["audit", "report"]
