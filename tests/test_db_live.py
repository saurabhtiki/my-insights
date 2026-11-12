"""The same import against real servers. Skipped unless a test server is given, e.g.

MY_INSIGHTS_TEST_POSTGRES=postgresql://postgres:pass@localhost:55432/insights_test
MY_INSIGHTS_TEST_MYSQL=mysql://root:pass@localhost:53306/insights_test

The user must be allowed to create tables: each run builds `customers`, `orders` and a view
(and, on Postgres, a `sales` schema) first.
"""

import os
from urllib.parse import unquote, urlsplit

import pytest

from my_insights import db_connect
from my_insights.db_loading import load_tables, refresh_from_server
from my_insights.linking import suggest_links
from my_insights.loading import UploadRejected
from my_insights.workspace import DbServer

pytestmark = pytest.mark.django_db

SETUP = {
    "postgres": [
        "DROP VIEW IF EXISTS big_orders",
        "DROP TABLE IF EXISTS orders",
        "DROP TABLE IF EXISTS customers",
        "DROP SCHEMA IF EXISTS sales CASCADE",
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, city TEXT, pin TEXT)",
        (
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),"
            " amount NUMERIC(10,2), ordered_on DATE, placed_at TIMESTAMPTZ, paid BOOLEAN, extra JSONB)"
        ),
        "INSERT INTO customers VALUES (1, 'Pune', '0411'), (2, 'Mumbai', '0400')",
        (
            "INSERT INTO orders VALUES (10, 1, 500.50, '2026-04-01', '2026-04-01 10:30+05:30', true,"
            " '{\"gift\": true}'), (11, 2, 300, '2026-04-02', NULL, false, NULL),"
            " (12, 1, 950, '2026-04-03', '2026-04-03 09:00+05:30', true, NULL)"
        ),
        "CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 400",
        "CREATE SCHEMA sales",
        "CREATE TABLE sales.targets (city TEXT, target INTEGER)",
        "INSERT INTO sales.targets VALUES ('Pune', 1000)",
        "ANALYZE",
    ],
    "mysql": [
        "DROP VIEW IF EXISTS big_orders",
        "DROP TABLE IF EXISTS orders",
        "DROP TABLE IF EXISTS customers",
        "CREATE TABLE customers (id INT PRIMARY KEY, city VARCHAR(50), pin VARCHAR(10))",
        (
            "CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT, amount DECIMAL(10,2),"
            " ordered_on DATE, placed_at DATETIME, paid BOOLEAN, extra JSON,"
            " FOREIGN KEY (customer_id) REFERENCES customers(id))"
        ),
        "INSERT INTO customers VALUES (1, 'Pune', '0411'), (2, 'Mumbai', '0400')",
        (
            "INSERT INTO orders VALUES (10, 1, 500.50, '2026-04-01', '2026-04-01 10:30', true,"
            " '{\"gift\": true}'), (11, 2, 300, '2026-04-02', NULL, false, NULL),"
            " (12, 1, 950, '2026-04-03', '2026-04-03 09:00', true, NULL)"
        ),
        "CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 400",
        "ANALYZE TABLE customers, orders",
    ],
}


def server_from_env(kind: str) -> DbServer | None:
    url = os.environ.get(f"MY_INSIGHTS_TEST_{kind.upper()}")
    if not url:
        return None
    parts = urlsplit(url)
    return DbServer(
        kind=kind,
        host=parts.hostname,
        port=parts.port or db_connect.DIALECTS[kind].default_port,
        database=parts.path.lstrip("/"),
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )


def run_as_admin(kind: str, server: DbServer, statements: list[str]) -> None:
    """Run statements with a normal (writing) connection: to build the test data."""
    dialect = db_connect.DIALECTS[kind]
    dialect.load()
    if kind == "postgres":
        with dialect.driver.connect(
            host=server.host,
            port=server.port,
            dbname=server.database,
            user=server.user,
            password=server.password,
            autocommit=True,
        ) as connection:
            for statement in statements:
                connection.execute(statement)
    else:
        connection = dialect.driver.connect(
            host=server.host,
            port=server.port,
            database=server.database,
            user=server.user,
            password=server.password,
            autocommit=True,
        )
        with connection, connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)


@pytest.fixture(params=["postgres", "mysql"])
def live(request):
    server = server_from_env(request.param)
    if server is None:
        pytest.skip(f"no MY_INSIGHTS_TEST_{request.param.upper()} test server given")
    run_as_admin(request.param, server, SETUP[request.param])
    return server


def test_list_import_keys_and_refresh(live, workspace):
    tables = {t.name: t for t in db_connect.list_tables(live)}
    assert {"customers", "orders", "big_orders"} <= set(tables)
    assert tables["big_orders"].kind == "view" and tables["big_orders"].row_count is None
    assert tables["orders"].columns[:2] == ["id", "customer_id"]
    if live.kind == "postgres":
        assert "sales.targets" in tables

    pending = workspace.add_pending(live.label, None, list(tables.values()), live)
    customers, orders, query = load_tables(
        workspace,
        pending.token,
        ["customers", "orders"],
        query_name="pune",
        query_sql="SELECT * FROM orders WHERE customer_id = 1",
    )
    kinds = {c.name: c.kind for c in orders.columns}
    assert kinds["amount"] == "number" and kinds["ordered_on"] == "date"
    assert kinds["placed_at"] == "datetime"
    # MySQL's BOOLEAN is really TINYINT(1): 0 / 1
    assert kinds["paid"] == ("boolean" if live.kind == "postgres" else "integer")
    assert {c.name: c.kind for c in customers.columns}["pin"] == "text"
    assert query.row_count == 2
    with workspace.lock:
        first = suggest_links(workspace)[0]
    assert (first.main_column, first.lookup_column, first.from_database) == (
        "customer_id",
        "id",
        True,
    )

    run_as_admin(live.kind, live, ["INSERT INTO orders (id, customer_id) VALUES (13, 2)"])
    assert refresh_from_server(workspace, orders) == []
    assert orders.row_count == 4


def test_nothing_can_be_changed(live):
    with db_connect.reading(live) as reader:
        with pytest.raises(reader.errors) as caught:
            if live.kind == "postgres":
                reader.connection.execute("DELETE FROM orders")
            else:
                with reader.connection.cursor() as cursor:
                    cursor.execute("DELETE FROM orders")
        assert "Only reading" in reader.message(caught.value)


def test_wrong_password_message(live):
    wrong = DbServer(live.kind, live.host, live.port, live.database, live.user, "wrong")
    with pytest.raises(UploadRejected, match="password is wrong"):
        db_connect.list_tables(wrong)
