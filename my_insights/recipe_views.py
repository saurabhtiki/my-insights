"""Recipe pages: save the setup, load a recipe (file or host), match its datasets, apply."""

import json
import logging
import re

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.module_loading import import_string
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_datatable import formula_safe, rows_csv_response

from . import conf, recipe_sources
from .exporting import attachment
from .exporting import file_name as export_file_name
from .recipe import (
    NAME_LENGTH,
    RecipeError,
    apply_recipe,
    candidates,
    file_name,
    guess_matches,
    match_problems,
    needed_columns,
    read_recipe,
    sources,
    to_recipe,
)
from .uploads import MemoryOnlyUploadHandler
from .views import page_context, panel_response
from .workspace import current_workspace

logger = logging.getLogger(__name__)


def _hook(setting: str):
    """The host's function named in a setting, or None when the host set none."""
    path = conf.get(setting)
    return import_string(path) if path else None


@require_POST
def recipe_save(request):
    workspace, expired = current_workspace(request)
    if workspace is None:
        return panel_response(request, None, [], expired)
    name = " ".join(request.POST.get("name", "").split())[:NAME_LENGTH] or "My recipe"
    recipe = to_recipe(workspace, name, keep_secrets=conf.get("RECIPE_KEEP_SECRETS"))
    save = _hook("RECIPE_SAVE")
    if save is None:
        response = HttpResponse(
            json.dumps(recipe, ensure_ascii=False, indent=1),
            content_type="application/json; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="{file_name(name)}"'
        logger.info("Recipe downloaded: %s", name)
        return response
    try:
        message = save(request, recipe, name)
    except Exception:  # the host's code: any failure becomes a message, never a crash
        logger.exception("The host's RECIPE_SAVE hook failed")
        result = {
            "ok": False,
            "title": "Not saved",
            "message": "Could not save the recipe: the app reported a problem. "
            "Please try again, or ask your administrator.",
        }
    else:
        logger.info("Recipe saved by the host: %s", name)
        result = {"ok": True, "title": "Saved", "message": message or f"Saved “{name}”."}
    return panel_response(request, workspace, [result])


@csrf_exempt  # CSRF is checked in _receive_recipe, after the memory-only handler is set
@require_POST
def recipe_upload(request):
    handler = MemoryOnlyUploadHandler(
        request, max_file_bytes=conf.get("RECIPE_MAX_KB") * 1024, max_files=1
    )
    request.upload_handlers = [handler]
    return _receive_recipe(request, handler)


@csrf_protect
def _receive_recipe(request, handler):
    uploaded = request.FILES.get("recipe")
    workspace, _ = current_workspace(request, create=True)
    try:
        if handler.too_big:
            raise RecipeError(
                f"The recipe is larger than {conf.get('RECIPE_MAX_KB')} KB, so it was not read."
            )
        if uploaded is None:
            raise RecipeError("Choose a recipe file (.json) to load.")
        try:
            raw = uploaded.read()
        finally:
            uploaded.close()
        _hold(workspace, read_recipe(raw))
    except RecipeError as error:
        workspace.notice = str(error)
        return redirect("my_insights:home")
    return redirect("my_insights:recipe_match")


@require_GET
def recipe_load(request):
    """The host's page links here with its own recipe id, e.g. /recipe/load/?id=7."""
    load = _hook("RECIPE_LOAD")
    if load is None:
        raise Http404("Loading recipes from the app is not set up.")
    recipe_id = request.GET.get("id", "").strip()
    workspace, _ = current_workspace(request, create=True)
    try:
        if not recipe_id:
            raise RecipeError("No recipe was chosen.")
        try:
            raw = load(request, recipe_id)
        except Exception as error:  # the host's code: any failure becomes a message
            logger.exception("The host's RECIPE_LOAD hook failed for %r", recipe_id)
            raise RecipeError(
                "Could not load the recipe: the app reported a problem. Please try again, "
                "or ask your administrator."
            ) from error
        if raw is None:
            raise RecipeError("That recipe was not found. It may have been deleted.")
        _hold(workspace, read_recipe(raw))
    except RecipeError as error:
        workspace.notice = str(error)
        return redirect("my_insights:home")
    return redirect("my_insights:recipe_match")


def _hold(workspace, recipe: dict) -> None:
    with workspace.lock:
        workspace.pending_recipe = recipe
    logger.info("Recipe waiting to be matched: %s", recipe["name"])


@require_http_methods(["GET", "POST"])
def recipe_match(request):
    workspace, _ = current_workspace(request)
    if workspace is None or workspace.pending_recipe is None:
        return redirect("my_insights:home")
    with workspace.lock:
        recipe = workspace.pending_recipe
        if recipe is None:  # applied or cancelled in another tab meanwhile
            return redirect("my_insights:home")
        if request.method == "POST" and request.POST.get("token") != recipe["token"]:
            # Another tab loaded another recipe: these picks were made for the old one.
            matches = guess_matches(recipe, workspace)
            context = _match_context(
                request,
                workspace,
                recipe,
                matches,
                "Another recipe was loaded meanwhile (in another tab). Check the picks below.",
            )
            return _render_match(request, context)
        applying = request.method == "POST" and request.POST.get("action") == "apply"
        if request.method == "POST":
            matches = _picked(request.POST, recipe)
            _keep_typed(request.POST, recipe)
        else:
            matches = guess_matches(recipe, workspace)
        if applying and recipe.get("applying"):
            context = _match_context(
                request,
                workspace,
                recipe,
                matches,
                "This recipe is being applied already (in another tab). Wait a moment.",
            )
            return _render_match(request, context)
        if not applying:
            context = _match_context(request, workspace, recipe, matches)
            return _render_match(request, context)
        recipe["applying"] = True
    # Applied without holding the lock: fetching an API / database may take a while, and the
    # user's other pages should keep working meanwhile. apply_recipe locks what it changes.
    try:
        notice = apply_recipe(workspace, recipe, matches, recipe["typed"])
    except RecipeError as error:
        logger.info("Recipe not applied: %s", error)
        with workspace.lock:
            context = _match_context(request, workspace, recipe, matches, str(error))
        return _render_match(request, context)
    finally:
        recipe["applying"] = False
    with workspace.lock:
        workspace.done_notice = notice
        if workspace.pending_recipe is recipe:
            workspace.pending_recipe = None
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("my_insights:home")})
    return redirect("my_insights:home")


def _render_match(request, context):
    if request.headers.get("HX-Request") == "true":
        return render(request, "my_insights/partials/recipe_match_form.html", context)
    return render(request, "my_insights/recipe.html", context)


def _picked(data, recipe: dict) -> dict[int, int | None]:
    picks = {}
    for entry in sources(recipe):
        value = data.get(f"match_{entry['id']}", "")
        connection = entry.get("connection")
        if (
            value == recipe_sources.FETCH
            and connection
            and not recipe_sources.switched_off(connection)
        ):
            picks[entry["id"]] = recipe_sources.FETCH
        else:
            picks[entry["id"]] = int(value) if re.fullmatch(r"[0-9]{1,9}", value) else None
    return picks


def _keep_typed(data, recipe: dict) -> None:
    """Remember typed keys / passwords (memory only, like a manual import) so the form can
    be redrawn without sending them back to the browser. Empty = keep what was typed before."""
    typed = recipe.setdefault("typed", {})  # {group: {secret key: value}}: one per API / server
    for entry in sources(recipe):
        connection = entry.get("connection")
        if connection is None:
            continue
        for number, field in enumerate(recipe_sources.secret_fields(connection)):
            value = data.get(f"secret_{entry['id']}_{number}", "")
            if value:
                group = recipe_sources.group_of(connection)
                typed.setdefault(group, {})[field["key"]] = value


def _match_context(request, workspace, recipe, matches, refused: str = "") -> dict:
    typed = recipe.setdefault("typed", {})
    problems = match_problems(recipe, workspace, matches, typed)
    rows, asked_in = [], {}  # group → the row that shows its key / password boxes
    for entry in sources(recipe):
        picked = matches.get(entry["id"])
        group = recipe_sources.group_of(entry["connection"]) if entry.get("connection") else ""
        shared_with = asked_in.get(group, "") if picked == recipe_sources.FETCH else ""
        if group and picked == recipe_sources.FETCH and not shared_with:
            asked_in[group] = entry["name"]
        rows.append(
            {
                "entry": entry,
                "picked": picked,
                "problem": problems.get(entry["id"], ""),
                "columns": needed_columns(entry),
                "fetch": _fetch_info(recipe, entry, typed.get(group, {}), shared_with),
            }
        )
    built = [e["name"] for e in recipe["datasets"] if e["kind"] != "source"]
    return page_context(
        request,
        workspace,
        False,
        recipe=recipe,
        rows=rows,
        options=candidates(workspace),
        all_fetched=all(row["picked"] == recipe_sources.FETCH for row in rows),
        ready=not problems,
        refused=refused,
        built_names=built,
        query_count=len(recipe["queries"]),
        chart_count=len(recipe["charts"]),
        dashboard_names=[d.name for d in recipe["dashboards"]],
        has_work=bool(workspace.queries or workspace.charts or workspace.dashboards),
    )


def _fetch_info(recipe: dict, entry: dict, typed: dict, shared_with: str) -> dict | None:
    """For the match screen: where the dataset came from and the secrets to type, if any.
    shared_with: an earlier row from the same API / server, whose boxes this row uses."""
    connection = entry.get("connection")
    if connection is None:
        return None
    if connection["type"] == "api_child":
        parent = next((e for e in sources(recipe) if e["id"] == connection["parent"]), None)
        return {
            "label": f"↻ Fetch again (comes with “{parent['name'] if parent else '?'}”)",
            "where": "",
            "off": recipe_sources.switched_off(connection),
            "fields": [],
        }
    fields = [
        field | {"number": number, "typed": bool(typed.get(field["key"]))}
        for number, field in enumerate(recipe_sources.secret_fields(connection))
    ]
    kind = "database" if connection["type"] == "database" else "API"
    return {
        "label": f"↻ Fetch again from saved {kind}",
        "where": recipe_sources.where_from(connection),
        "off": recipe_sources.switched_off(connection),
        "fields": fields,
        "shared_with": shared_with,
        "missing": [] if shared_with else [f for f in fields if not f["saved"]],
        "saved": [] if shared_with else [f for f in fields if f["saved"]],
        "any_typed": any(f["typed"] for f in fields if f["saved"]),
    }


@require_GET
def recipe_template(request):
    """An empty CSV with the columns one recipe dataset needs, e.g. ?id=1 →
    sales_template.csv holding "Date,Branch,Amount": fill it in and upload it."""
    workspace, _ = current_workspace(request)
    recipe = workspace.pending_recipe if workspace is not None else None
    if recipe is None:
        return redirect("my_insights:home")
    entry_id = request.GET.get("id", "")
    entry = next((e for e in sources(recipe) if str(e["id"]) == entry_id), None)
    if entry is None:
        logger.info("Template asked for an unknown recipe dataset %r", entry_id[:20])
        raise Http404("That recipe dataset was not found.")
    headings = [formula_safe(name) for name, _ in needed_columns(entry)]
    response = rows_csv_response("template.csv", headings, [])
    return attachment(response, export_file_name(f"{entry['name']} template", "csv"))


@require_POST
def recipe_cancel(request):
    workspace, _ = current_workspace(request)
    if workspace is not None:
        workspace.pending_recipe = None
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("my_insights:home")})
    return redirect("my_insights:home")
