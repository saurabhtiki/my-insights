"""A saved query's answer kept as a dataset, so another query can ask about it.

    Query "Top customers" (Sum of Amount by Customer, top 20)
    → dataset "Top customers (result)" with columns Customer, Sum of Amount
    → a new query on it, or link it to the sales data.

The answer is copied into an in-memory DuckDB table (a view can't keep the query's typed
filter values) and copied again each time the query is saved. Nothing goes to disk.
"""

import copy
import logging

import duckdb

from . import conf, linking
from .loading import fill_samples, quote_name
from .querying import field_catalog, plan_query
from .workspace import ColumnProfile, Dataset, ServerBusy, check_capacity

logger = logging.getLogger(__name__)


class QueryDatasetFailed(Exception):
    """The query's answer could not be made into a dataset; the message is safe to show."""


def make_query_dataset(workspace, query_id: int) -> Dataset:
    """A new dataset from a saved query's answer. Raises QueryDatasetFailed (also when the
    server's memory budget is full, as for an upload)."""
    try:
        check_capacity()
    except ServerBusy as error:
        raise QueryDatasetFailed(str(error)) from error
    saved = workspace.queries[query_id]
    dataset_id = workspace.new_dataset_id()
    dataset = Dataset(
        dataset_id=dataset_id,
        name=workspace.unique_name(f"{saved.name} (result)"[:100]),
        table_name=f"ds_{dataset_id}",
        source_file=saved.name,
        row_count=0,
        columns=[],
        query_id=query_id,
    )
    _fill(workspace, dataset)
    workspace.datasets[dataset_id] = dataset
    logger.info("Dataset %s made from query %s", dataset_id, query_id)
    return dataset


def refresh_from_query(workspace, query_id: int) -> list[str]:
    """Copy a just-saved query's answer into the datasets made from it again (and into the
    query-datasets built on those). Returns the problems ([] when none)."""
    problems = []
    for dataset in [d for d in workspace.datasets.values() if d.query_id == query_id]:
        problems += _refresh([dataset, *workspace.dependants(dataset.dataset_id)], workspace)
    return problems


def refresh_built_on(workspace, dataset_id: int) -> list[str]:
    """After a dataset changed (e.g. a link was edited): copy again the query-datasets built
    on it."""
    return _refresh(workspace.dependants(dataset_id), workspace)


def _refresh(datasets, workspace) -> list[str]:
    problems = []
    for dataset in datasets:  # each after the ones it is built from
        try:
            if dataset.query_id is not None:
                _fill(workspace, dataset)
            elif dataset.link is not None:  # a combined dataset: its columns may have changed
                linking.refresh_view(workspace, dataset)
        except (QueryDatasetFailed, linking.LinkRejected) as error:
            problems.append(f"“{dataset.name}” was not updated: {error}")
    return problems


def _fill(workspace, dataset: Dataset) -> None:
    """(Re)build the dataset's table from its query, in one transaction: a failure leaves
    the dataset as it was."""
    saved = workspace.queries.get(dataset.query_id)
    source = workspace.datasets.get(saved.spec.dataset_id) if saved else None
    if saved is None or source is None:
        raise QueryDatasetFailed("its query or the query's dataset is gone.")
    max_rows = int(conf.get("MAX_ROWS"))
    spec = copy.deepcopy(saved.spec)
    plan = plan_query(spec, source, catalog=field_catalog(source), cap=max_rows)
    if not plan.ok:
        raise QueryDatasetFailed(" ".join(plan.problems) or "the query has nothing to show yet.")
    columns, sql, params = plan.columns, plan.sql, plan.params
    if plan.across is not None:  # Across becomes a column (the cells): columns never change
        groups = [column for column in plan.columns if not column.is_value]
        values = [column for column in plan.columns if column.is_value]
        columns, sql, params = [*groups, plan.across, *values], plan.cell_sql, plan.cell_params
    names = _unique([column.label for column in columns])
    select = ", ".join(
        f"{column.key} AS {quote_name(name)}" for column, name in zip(columns, names, strict=True)
    )
    building = f"{dataset.table_name}_new"
    connection = workspace.connection
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            f"CREATE OR REPLACE TABLE {building} AS SELECT {select} FROM ({sql}) AS result", params
        )
        row_count = connection.execute(f"SELECT count(*) FROM {building}").fetchall()[0][0]
        if row_count > max_rows:
            raise QueryDatasetFailed(
                f"the answer has more than {max_rows:,} rows. Add a filter or group by fewer fields."
            )
        filled = connection.execute(
            f"SELECT {', '.join(f'count({quote_name(name)})' for name in names)} FROM {building}"
        ).fetchall()[0]
        connection.execute(f"DROP TABLE IF EXISTS {dataset.table_name}")
        connection.execute(f"ALTER TABLE {building} RENAME TO {dataset.table_name}")
        connection.execute("COMMIT")
    except duckdb.OutOfMemoryException as error:
        connection.execute("ROLLBACK")
        logger.warning("Out of memory making a query-dataset: %s", error)
        raise QueryDatasetFailed("it needs more memory than allowed per user.") from error
    except duckdb.Error as error:
        connection.execute("ROLLBACK")
        logger.exception("Could not make query %s into a dataset", saved.query_id)
        raise QueryDatasetFailed(f"it could not be worked out ({error}).") from error
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    dataset.columns = [
        ColumnProfile(name, column.kind, row_count - count)
        for column, name, count in zip(columns, names, filled, strict=True)
    ]
    fill_samples(connection, dataset.table_name, dataset.columns)
    dataset.row_count = row_count
    dataset.source_file = saved.name
    workspace.query_result_cache = None
    workspace.refresh_memory()


def _unique(labels: list[str]) -> list[str]:
    """Column names as headings, made unique: Amount, Amount (2). [ ] become ( ), as a
    formula can't name a field with brackets."""
    taken, names = set(), []
    for label in labels:
        label = label.replace("[", "(").replace("]", ")")
        name, number = label, 2
        while name.casefold() in taken:
            name = f"{label} ({number})"
            number += 1
        taken.add(name.casefold())
        names.append(name)
    return names
