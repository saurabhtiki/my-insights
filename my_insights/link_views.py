"""The link screen: main dataset + lookup lists → one combined dataset (see linking.py).

The whole form is posted on every change and drawn again by the server (htmx), so the
screen always shows the server's own check: match counts, duplicates and a preview.
"""

import copy
import dataclasses
import logging
import secrets

import duckdb
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods
from django_datatable import table_response

from . import linking, query_dataset
from .linking import JOIN_TYPES, LinkRejected
from .views import PREVIEW_ROWS, home_after_expiry, name_error, page_context, rows_table
from .workspace import LinkSpec, LinkStep, current_workspace

logger = logging.getLogger(__name__)

MAX_STEPS = 20
MAX_PAIRS = 10
MAX_DRAFTS = 5  # link screens remembered per user (browser tabs)
PREVIEW_TABLE_ID = "insights-link-preview"


def _number(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@require_http_methods(["GET", "POST"])
def link(request):
    workspace, expired = current_workspace(request)
    if workspace is None or len(workspace.datasets) < 2:
        target = home_after_expiry() if expired else redirect("my_insights:home")
        if request.headers.get("HX-Request") == "true":  # leave the page, don't swap it in
            return HttpResponse(status=204, headers={"HX-Redirect": target.url})
        return target
    draft_id = request.POST.get("draft", "")
    if not draft_id.isalnum() or len(draft_id) > 32:
        draft_id = secrets.token_hex(8)

    with workspace.lock:
        if request.method == "POST":
            spec, name, edit_id, action = _read_form(request.POST, workspace)
        else:
            spec, name, edit_id = _start_spec(request.GET, workspace)
            action = ""
        _fill_guesses(spec, workspace.datasets)
        exclude = set()
        if edit_id is not None:
            exclude = {edit_id, *(d.dataset_id for d in workspace.dependants(edit_id))}
        check = linking.check_link(workspace.connection, spec, workspace.datasets, exclude)
        _remember_draft(workspace, draft_id, check)
        suggested_name = linking.default_name(spec, workspace.datasets) if spec.steps else ""

        name_problem = ""
        if action == "create" and check.ok:
            final_name = name or suggested_name
            name_problem = name_error(workspace, final_name, ignore_id=edit_id)
            if not name_problem:
                try:
                    dataset = linking.save_link(workspace, spec, final_name, edit_id)
                except LinkRejected as error:
                    logger.info("Link refused: %s", error)
                    check.problems.append(str(error))
                else:
                    workspace.link_drafts.pop(draft_id, None)
                    # query-datasets built on it hold a copy: copy them again
                    for problem in query_dataset.refresh_built_on(workspace, dataset.dataset_id):
                        logger.warning("After a link change: %s", problem)
                    url = reverse("my_insights:dataset", args=[dataset.dataset_id])
                    if request.headers.get("HX-Request") == "true":
                        return HttpResponse(status=204, headers={"HX-Redirect": url})
                    return redirect(url)
            else:
                logger.info("Link name refused: %s", name_problem)
        if check.problems:
            logger.info("Link problems shown: %s", check.problems)

        context = page_context(
            request,
            workspace,
            False,
            spec=spec,
            draft_id=draft_id,
            name=name,
            suggested_name=suggested_name,
            name_problem=name_problem,
            edit_id=edit_id,
            check=check,
            steps=_steps_view(spec, check, workspace.datasets),
            choices=[d for d in workspace.datasets.values() if d.dataset_id not in exclude],
            main=workspace.datasets.get(spec.main_id),
            join_types=[(key, label, tip) for key, (_, label, tip) in JOIN_TYPES.items()],
            preview=_preview_table(request, workspace, check, draft_id),
            preview_rows=PREVIEW_ROWS,
        )
    if request.headers.get("HX-Request") == "true":
        return render(request, "my_insights/partials/link_form.html", context)
    return render(request, "my_insights/link.html", context)


def _start_spec(params, workspace) -> tuple[LinkSpec, str, int | None]:
    """First settings for the screen: an existing link to edit, or a suggested one."""
    edit = workspace.datasets.get(_number(params.get("edit")))
    if edit is not None and edit.link is not None:
        return copy.deepcopy(edit.link), edit.name, edit.dataset_id
    main_id = _number(params.get("main"))
    if main_id not in workspace.datasets:
        uploaded = [d for d in workspace.datasets.values() if d.link is None]
        main_id = max(uploaded or workspace.datasets.values(), key=lambda d: d.row_count).dataset_id
    spec = linking.suggested_spec(main_id, linking.suggest_links(workspace))
    if not spec.steps:
        spec.steps.append(LinkStep(lookup_id=0))
    return spec, "", None


def _read_form(data, workspace) -> tuple[LinkSpec, str, int | None, str]:
    """The posted form → settings, with the clicked button's change applied."""
    main_id = _number(data.get("main"))
    main_changed = main_id != _number(data.get("previous_main"), main_id)
    spec = LinkSpec(main_id=main_id, ignore_case=data.get("ignore_case") == "on")
    for i in range(min(_number(data.get("step_count")), MAX_STEPS)):
        lookup_id = _number(data.get(f"s{i}_lookup"))
        linked_to = _number(data.get(f"s{i}_to"))
        pairs = [
            (data.get(f"s{i}_p{j}_left", ""), data.get(f"s{i}_p{j}_right", ""))
            for j in range(min(_number(data.get(f"s{i}_pairs")), MAX_PAIRS))
        ]
        moved = (
            lookup_id != _number(data.get(f"s{i}_previous_lookup"), lookup_id)
            or linked_to != _number(data.get(f"s{i}_previous_to"), linked_to)
            or (main_changed and linked_to == 0)
        )
        join_type = data.get(f"s{i}_join")
        spec.steps.append(
            LinkStep(
                lookup_id=lookup_id,
                linked_to=linked_to,
                pairs=[] if moved else pairs,  # other datasets: guess the columns again
                join_type=join_type if join_type in JOIN_TYPES else "left",
            )
        )
    edit = workspace.datasets.get(_number(data.get("edit")))
    edit_id = edit.dataset_id if edit is not None and edit.link is not None else None
    action = data.get("action", "")
    _apply_action(spec, action)
    name = " ".join(data.get("name", "").split())
    return spec, name, edit_id, action


def _apply_action(spec: LinkSpec, action: str) -> None:
    kind, _, where = action.partition(":")
    parts = [_number(part, -1) for part in where.split(":")] if where else []
    steps = spec.steps
    if kind == "add_step" and len(steps) < MAX_STEPS:
        steps.append(LinkStep(lookup_id=0))
    elif kind == "remove_step" and parts and 0 <= parts[0] < len(steps):
        removed = parts[0] + 1  # its number in "linked to"
        del steps[parts[0]]
        for step in steps:
            if step.linked_to == removed:
                step.linked_to, step.pairs = 0, []
            elif step.linked_to > removed:
                step.linked_to -= 1
    elif kind == "add_pair" and parts and 0 <= parts[0] < len(steps):
        if len(steps[parts[0]].pairs) < MAX_PAIRS:
            steps[parts[0]].pairs.append(("", ""))
    elif kind == "remove_pair" and len(parts) == 2 and 0 <= parts[0] < len(steps):
        pairs = steps[parts[0]].pairs
        if 0 <= parts[1] < len(pairs) and len(pairs) > 1:
            del pairs[parts[1]]


def _fill_guesses(spec: LinkSpec, datasets) -> None:
    """A lookup with no link columns yet gets the likeliest pair by name, or an empty row."""
    for index, step in enumerate(spec.steps, start=1):
        if step.pairs:
            continue
        lookup = datasets.get(step.lookup_id)
        linked_id = spec.main_id if step.linked_to == 0 else None
        if 0 < step.linked_to < index:
            linked_id = spec.steps[step.linked_to - 1].lookup_id
        linked = datasets.get(linked_id)
        guess = linking.guess_pair(linked, lookup) if lookup and linked else None
        step.pairs = [guess or ("", "")]


def _steps_view(spec: LinkSpec, check, datasets) -> list[dict]:
    main = datasets.get(spec.main_id)
    views = []
    for i, step in enumerate(spec.steps):
        options = [(0, f"{main.name} (main)" if main else "Main dataset")]
        options += [
            (k, datasets[s.lookup_id].name)
            for k, s in enumerate(spec.steps[:i], start=1)
            if s.lookup_id in datasets
        ]
        if step.linked_to == 0:
            linked = main
        elif step.linked_to <= i:
            linked = datasets.get(spec.steps[step.linked_to - 1].lookup_id)
        else:
            linked = None
        views.append(
            {
                "index": i,
                "number": i + 1,
                "step": step,
                "lookup": datasets.get(step.lookup_id),
                "linked": linked,
                "linked_options": options,
                "pairs": list(enumerate(step.pairs)),
                "check": check.steps[i] if i < len(check.steps) else None,
            }
        )
    return views


def _remember_draft(workspace, draft_id: str, check) -> None:
    drafts = workspace.link_drafts
    drafts.pop(draft_id, None)
    drafts[draft_id] = check  # newest last
    while len(drafts) > MAX_DRAFTS:
        drafts.pop(next(iter(drafts)))


def _preview_table(request, workspace, check, draft_id: str):
    """The first rows the combined dataset would have, or None when it can't be made yet."""
    if not check.ok:
        return None
    try:
        records = linking.preview_rows(workspace.connection, check.sql, PREVIEW_ROWS)
    except duckdb.Error:
        logger.exception("Could not read the link preview")
        return None
    profiles = [dataclasses.replace(column.profile, name=column.name) for column in check.columns]
    return rows_table(
        request,
        records,
        profiles,
        PREVIEW_TABLE_ID,
        reverse("my_insights:link_preview", args=[draft_id]),
        push_url=False,
    )


@require_GET
def link_preview(request, draft_id: str):
    """Sort / filter / page clicks inside the preview table."""
    workspace, _ = current_workspace(request)
    check = workspace.link_drafts.get(draft_id) if workspace else None
    if check is None:
        return HttpResponse(
            '<p class="text-sm text-base-content/60">The preview has expired. '
            "Change any setting above to see it again.</p>"
        )
    with workspace.lock:
        table = _preview_table(request, workspace, check, draft_id)
    if table is None:
        return HttpResponse('<p class="text-sm text-base-content/60">No preview yet.</p>')
    context = {"table": table}
    return table_response(
        request,
        table,
        "my_insights/partials/link_preview.html",
        "my_insights/partials/link_preview.html",
        context,
    )


@require_GET
def link_suggestions(request):
    """The "these datasets can be linked" hints on the home page (loaded after the page)."""
    workspace, _ = current_workspace(request)
    groups = []
    if workspace is not None:
        with workspace.lock:
            try:
                groups = linking.suggestion_groups(workspace)
            except duckdb.Error:
                logger.exception("Could not work out link suggestions")
    return render(
        request,
        "my_insights/partials/link_suggestions.html",
        {"groups": groups},
    )
