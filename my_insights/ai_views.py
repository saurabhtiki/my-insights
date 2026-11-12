"""The "Ask AI" page: the service's settings, Notes for AI and the question box.

A good answer opens the query screen filled in (?ai=<key>), where the user checks it and
presses Save. The key is kept in the in-memory workspace only and never sent back to the page.
"""

import logging

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import ai_assist, conf
from .ai_assist import MAX_NOTES, MAX_QUESTION, AiError, AiSettings
from .views import home_after_expiry, page_context
from .workspace import current_workspace

logger = logging.getLogger(__name__)


def _workspace_or_none(request):
    """(workspace, response): the response sends the user home when there is no data."""
    if not conf.get("ALLOW_AI"):
        raise Http404("The AI assistant is switched off on this server.")
    workspace, expired = current_workspace(request)
    if workspace is not None and workspace.datasets:
        return workspace, None
    target = home_after_expiry() if expired else redirect("my_insights:home")
    if request.headers.get("HX-Request") == "true":
        return None, HttpResponse(status=204, headers={"HX-Redirect": target.url})
    return None, target


def _ai_context(workspace, **extra) -> dict:
    settings = workspace.ai_settings
    return {
        "ai_settings": settings,
        "ai_has_key": bool(settings and settings.key),
        "ai_notes": workspace.ai_notes,
        "ai_history": [question for question, _ in workspace.ai_history],
        "max_question": MAX_QUESTION,
        "max_notes": MAX_NOTES,
        **extra,
    }


@require_GET
def ai_page(request):
    workspace, response = _workspace_or_none(request)
    if response:
        return response
    with workspace.lock:
        hint = _hint(request.GET, workspace)
        context = page_context(
            request, workspace, False, **_ai_context(workspace, hint=hint, question="")
        )
    return render(request, "my_insights/ai.html", context)


def _settings_from(post, saved: AiSettings | None) -> AiSettings:
    """The posted settings. An empty key box keeps the key already saved, but only for the
    same address: the key is never sent to a service it wasn't typed for."""
    address = ai_assist.clean_address(post.get("address", ""))
    key = post.get("key", "").strip()
    if not key and saved is not None and saved.address == address:
        key = saved.key
    return AiSettings(address, key, " ".join(post.get("model", "").split())[:200])


@require_POST
def ai_settings_save(request):
    workspace, response = _workspace_or_none(request)
    if response:
        return response
    with workspace.lock:
        settings = _settings_from(request.POST, workspace.ai_settings)
        problem = ""
        try:
            ai_assist.check_address(settings.address)
        except AiError as error:
            problem = str(error)
            logger.info("AI settings refused: %s", problem)
        if not problem and not settings.model:
            problem = "Pick or type a model, e.g. gpt-4o-mini."
        if not problem:
            workspace.ai_settings = settings
            logger.info("AI settings saved: %s", settings.label)
        context = _ai_context(workspace, problem=problem, typed=settings if problem else None)
    return render(request, "my_insights/partials/ai_settings.html", context)


@require_POST
def ai_forget(request):
    workspace, response = _workspace_or_none(request)
    if response:
        return response
    with workspace.lock:
        workspace.ai_settings = None
        context = _ai_context(workspace, done="Forgotten: the address, key and model are gone.")
    return render(request, "my_insights/partials/ai_settings.html", context)


@require_POST
def ai_models(request):
    """The service's model names, as <option>s for the Model box's list."""
    workspace, response = _workspace_or_none(request)
    if response:
        return response
    with workspace.lock:
        settings = _settings_from(request.POST, workspace.ai_settings)
    try:
        models, problem = ai_assist.list_models(settings), ""
    except AiError as error:
        models, problem = [], str(error)
        logger.info("AI models not listed: %s", problem)
    return render(
        request, "my_insights/partials/ai_models.html", {"models": models, "problem": problem}
    )


@require_POST
def ai_new_topic(request):
    workspace, response = _workspace_or_none(request)
    if response:
        return response
    with workspace.lock:
        workspace.ai_history.clear()
        if "notes" in request.POST:  # keep notes typed but not sent yet
            workspace.ai_notes = request.POST["notes"].strip()[:MAX_NOTES]
        context = _ai_context(
            workspace,
            hint=_hint(request.POST, workspace),
            question="",
            done="New topic: earlier questions are forgotten.",
            datasets=list(workspace.datasets.values()),
        )
    return render(request, "my_insights/partials/ai_question.html", context)


def _hint(params, workspace) -> int | None:
    """The dataset the user pointed the AI at, if it is still loaded."""
    value = params.get("dataset", "")
    return int(value) if value.isdigit() and int(value) in workspace.datasets else None


@require_POST
def ai_draft(request):
    """Ask the AI. Good answer → the query screen filled in; else the problem, question kept."""
    workspace, response = _workspace_or_none(request)
    if response:
        return response
    question = " ".join(request.POST.get("question", "").split())
    with workspace.lock:
        if "notes" in request.POST:
            workspace.ai_notes = request.POST["notes"].strip()[:MAX_NOTES]
        hint = _hint(request.POST, workspace)
        settings = workspace.ai_settings
        problem = ""
        if settings is None:
            problem = "Save the AI service's address, key and model first (Step 1)."
        elif not question:
            problem = "Type a question, e.g. Total sales by branch for this financial year."
        elif len(question) > MAX_QUESTION:
            problem = f"Keep the question under {MAX_QUESTION:,} characters."
        else:
            hint_name = workspace.datasets[hint].name if hint else ""
            messages = ai_assist.build_messages(workspace, question, hint_name)
    if not problem:
        try:
            answer_text = ai_assist.ask(settings, messages)  # no lock: this may take a while
            answer = ai_assist.parse_answer(answer_text)
            with workspace.lock:
                draft = ai_assist.to_draft(workspace, answer, question)
                ai_assist.remember(workspace, question, answer)
                key = ai_assist.keep_draft(workspace, draft)
        except AiError as error:
            problem = str(error)
    if not problem:
        logger.info("AI drafted a query on dataset %s", draft.spec.dataset_id)
        url = reverse("my_insights:query") + f"?ai={key}"
        if draft.chart_type:
            url += "&tab=chart"
        return HttpResponse(status=204, headers={"HX-Redirect": url})
    logger.info("AI draft not made: %s", problem)
    with workspace.lock:
        context = _ai_context(
            workspace,
            hint=hint,
            question=question,
            problem=problem,
            datasets=list(workspace.datasets.values()),
        )
    template = (
        "my_insights/partials/ai_followup.html"
        if request.POST.get("from") == "query"
        else "my_insights/partials/ai_question.html"
    )
    return render(request, template, context)
