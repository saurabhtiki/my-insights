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

from . import conf
from .recipe import (
    NAME_LENGTH,
    RecipeError,
    apply_recipe,
    candidates,
    file_name,
    guess_matches,
    match_problems,
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
    recipe = to_recipe(workspace, name)
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
        if request.method == "POST":
            matches = _picked(request.POST, recipe)
            if request.POST.get("action") == "apply":
                try:
                    workspace.done_notice = apply_recipe(workspace, recipe, matches)
                except RecipeError as error:
                    logger.info("Recipe not applied: %s", error)
                    context = _match_context(request, workspace, recipe, matches, str(error))
                    return _render_match(request, context)
                workspace.pending_recipe = None
                if request.headers.get("HX-Request") == "true":
                    return HttpResponse(
                        status=204, headers={"HX-Redirect": reverse("my_insights:home")}
                    )
                return redirect("my_insights:home")
        else:
            matches = guess_matches(recipe, workspace)
        context = _match_context(request, workspace, recipe, matches)
    return _render_match(request, context)


def _render_match(request, context):
    if request.headers.get("HX-Request") == "true":
        return render(request, "my_insights/partials/recipe_match_form.html", context)
    return render(request, "my_insights/recipe.html", context)


def _picked(data, recipe: dict) -> dict[int, int | None]:
    picks = {}
    for entry in sources(recipe):
        value = data.get(f"match_{entry['id']}", "")
        picks[entry["id"]] = int(value) if re.fullmatch(r"[0-9]{1,9}", value) else None
    return picks


def _match_context(request, workspace, recipe, matches, refused: str = "") -> dict:
    problems = match_problems(recipe, workspace, matches)
    rows = [
        {
            "entry": entry,
            "picked": matches.get(entry["id"]),
            "problem": problems.get(entry["id"], ""),
        }
        for entry in sources(recipe)
    ]
    built = [e["name"] for e in recipe["datasets"] if e["kind"] != "source"]
    return page_context(
        request,
        workspace,
        False,
        recipe=recipe,
        rows=rows,
        options=candidates(workspace),
        ready=not problems,
        refused=refused,
        built_names=built,
        query_count=len(recipe["queries"]),
        chart_count=len(recipe["charts"]),
        dashboard_names=[d.name for d in recipe["dashboards"]],
        has_work=bool(workspace.queries or workspace.charts or workspace.dashboards),
    )


@require_POST
def recipe_cancel(request):
    workspace, _ = current_workspace(request)
    if workspace is not None:
        workspace.pending_recipe = None
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("my_insights:home")})
    return redirect("my_insights:home")
