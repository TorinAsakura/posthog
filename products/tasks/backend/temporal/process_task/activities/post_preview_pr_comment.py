from django.core.exceptions import ObjectDoesNotExist

import structlog
from temporalio import activity

from posthog.dataclasses import frozen
from posthog.models.github_integration_base import PullRequestRef
from posthog.models.integration import GitHubIntegration
from posthog.models.user_integration import UserGitHubIntegration
from posthog.temporal.common.utils import close_db_connections
from posthog.utils import absolute_uri

from products.tasks.backend.constants import DEV_STACK_PREVIEW_STATE_KEY
from products.tasks.backend.models import TaskRun
from products.tasks.backend.temporal.observability import emit_agent_log, log_activity_execution
from products.tasks.backend.temporal.process_task.activities.get_pr_context import (
    get_github_integration,
    get_user_github_integration,
)
from products.tasks.backend.temporal.process_task.activities.get_task_processing_context import TaskProcessingContext

logger = structlog.get_logger(__name__)

PREVIEW_COMMENT_MARKER = "<!-- posthog-code-preview -->"

PREVIEW_COMMENT_ID_KEY = "pr_comment_id"

_COMMENTS_ENDPOINT = "/repos/{owner}/{repo}/issues/{issue_number}/comments"
_COMMENT_ENDPOINT = "/repos/{owner}/{repo}/issues/comments/{comment_id}"

_COMMENT_PAGE_SIZE = 100
_COMMENT_MAX_PAGES = 3

_PREVIEW_ENDED_BODY = f"""{PREVIEW_COMMENT_MARKER}
**PostHog preview**

This preview has ended. Rerun the task to start a new one."""


@frozen
class PostPreviewPrCommentInput:
    context: TaskProcessingContext
    pr_url: str


@frozen
class PostPreviewPrCommentOutput:
    posted: bool
    comment_id: int | None = None
    failure: str | None = None


@frozen
class EndPreviewPrCommentInput:
    context: TaskProcessingContext


@frozen
class EndPreviewPrCommentOutput:
    updated: bool
    failure: str | None = None


def build_preview_comment_body(*, team_id: int, task_id: str, run_id: str) -> str:
    preview_url = absolute_uri(f"/api/projects/{team_id}/tasks/{task_id}/runs/{run_id}/preview/")
    return f"""{PREVIEW_COMMENT_MARKER}
**PostHog preview**

{preview_url}

Sign in to PostHog with access to this project to open it.
The preview stays up while this cloud run is alive."""


def _preview_state(run_state: dict | None) -> dict | None:
    preview = (run_state or {}).get(DEV_STACK_PREVIEW_STATE_KEY)
    return preview if isinstance(preview, dict) else None


def _recorded_comment_id(run_state: dict | None) -> int | None:
    preview = _preview_state(run_state)
    if preview is None:
        return None
    comment_id = preview.get(PREVIEW_COMMENT_ID_KEY)
    return comment_id if isinstance(comment_id, int) and not isinstance(comment_id, bool) else None


def _record_comment_id(run_id: str, comment_id: int) -> None:
    def _mutator(state: dict) -> None:
        preview = state.get(DEV_STACK_PREVIEW_STATE_KEY)
        if not isinstance(preview, dict):
            return
        state[DEV_STACK_PREVIEW_STATE_KEY] = {**preview, PREVIEW_COMMENT_ID_KEY: comment_id}

    TaskRun.mutate_state_atomic(run_id, _mutator)


def _github_integration(ctx: TaskProcessingContext) -> GitHubIntegration | UserGitHubIntegration | None:
    try:
        if ctx.github_integration_id:
            return get_github_integration(ctx.github_integration_id)
        if ctx.github_user_integration_id:
            return get_user_github_integration(str(ctx.github_user_integration_id))
    except ObjectDoesNotExist:
        return None
    return None


def _pull_request_ref(github: GitHubIntegration | UserGitHubIntegration, pr_url: str | None) -> PullRequestRef | None:
    if not pr_url:
        return None
    return github.parse_pull_request_url(pr_url)


def _existing_preview_comment_id(github: GitHubIntegration | UserGitHubIntegration, ref: PullRequestRef) -> int | None:
    """Id of a preview comment already on this pull request, if one is there.

    The recorded id lives in run state, which a fresh run of the same task does not carry,
    so a rerun would otherwise stack a second preview comment on the same pull request.
    """
    for page in range(1, _COMMENT_MAX_PAGES + 1):
        response = github.api_request(
            "GET",
            f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
            endpoint=_COMMENTS_ENDPOINT,
            params={"per_page": _COMMENT_PAGE_SIZE, "page": page},
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, list):
            return None
        for comment in payload:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            comment_id = comment.get("id")
            if (
                isinstance(body, str)
                and PREVIEW_COMMENT_MARKER in body
                and isinstance(comment_id, int)
                and not isinstance(comment_id, bool)
            ):
                return comment_id
        if len(payload) < _COMMENT_PAGE_SIZE:
            return None
    return None


@activity.defn
@close_db_connections
def post_preview_pr_comment(input: PostPreviewPrCommentInput) -> PostPreviewPrCommentOutput:
    ctx = input.context
    with log_activity_execution("post_preview_pr_comment", **ctx.to_log_context()):
        try:
            task_run = TaskRun.objects.get(id=ctx.run_id)
        except TaskRun.DoesNotExist:
            return PostPreviewPrCommentOutput(posted=False, failure="run_not_found")

        run_state = task_run.state if isinstance(task_run.state, dict) else {}
        if _preview_state(run_state) is None:
            return PostPreviewPrCommentOutput(posted=False, failure=None)

        already_posted = _recorded_comment_id(run_state)
        if already_posted is not None:
            return PostPreviewPrCommentOutput(posted=False, comment_id=already_posted)

        github = _github_integration(ctx)
        if github is None:
            return PostPreviewPrCommentOutput(posted=False, failure="no_github_integration")

        ref = _pull_request_ref(github, input.pr_url)
        if ref is None:
            return PostPreviewPrCommentOutput(posted=False, failure="invalid_pr_url")

        try:
            reusable_comment_id = _existing_preview_comment_id(github, ref)
        except Exception:
            logger.warning("post_preview_pr_comment_lookup_failed", run_id=ctx.run_id, exc_info=True)
            reusable_comment_id = None

        if reusable_comment_id is not None:
            try:
                _record_comment_id(ctx.run_id, reusable_comment_id)
            except Exception:
                logger.warning("post_preview_pr_comment_state_write_failed", run_id=ctx.run_id, exc_info=True)
                return PostPreviewPrCommentOutput(
                    posted=False, comment_id=reusable_comment_id, failure="state_write_failed"
                )
            return PostPreviewPrCommentOutput(posted=False, comment_id=reusable_comment_id)

        body = build_preview_comment_body(team_id=ctx.team_id, task_id=ctx.task_id, run_id=ctx.run_id)
        try:
            response = github.api_request(
                "POST",
                f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
                endpoint=_COMMENTS_ENDPOINT,
                json_body={"body": body},
            )
        except Exception:
            logger.warning("post_preview_pr_comment_request_failed", run_id=ctx.run_id, exc_info=True)
            emit_agent_log(ctx.run_id, "warn", "Could not add the preview link to the pull request.")
            return PostPreviewPrCommentOutput(posted=False, failure="github_unavailable")

        if response.status_code != 201:
            logger.warning(
                "post_preview_pr_comment_rejected",
                run_id=ctx.run_id,
                status_code=response.status_code,
            )
            emit_agent_log(ctx.run_id, "warn", "Could not add the preview link to the pull request.")
            return PostPreviewPrCommentOutput(posted=False, failure=f"status_{response.status_code}")

        try:
            comment_id = response.json().get("id")
        except Exception:
            comment_id = None
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            return PostPreviewPrCommentOutput(posted=True, failure="missing_comment_id")

        try:
            _record_comment_id(ctx.run_id, comment_id)
        except Exception:
            logger.warning("post_preview_pr_comment_state_write_failed", run_id=ctx.run_id, exc_info=True)
            return PostPreviewPrCommentOutput(posted=True, comment_id=comment_id, failure="state_write_failed")

        return PostPreviewPrCommentOutput(posted=True, comment_id=comment_id)


@activity.defn
@close_db_connections
def end_preview_pr_comment(input: EndPreviewPrCommentInput) -> EndPreviewPrCommentOutput:
    ctx = input.context
    with log_activity_execution("end_preview_pr_comment", **ctx.to_log_context()):
        try:
            task_run = TaskRun.objects.get(id=ctx.run_id)
        except TaskRun.DoesNotExist:
            return EndPreviewPrCommentOutput(updated=False, failure="run_not_found")

        comment_id = _recorded_comment_id(task_run.state if isinstance(task_run.state, dict) else {})
        if comment_id is None:
            return EndPreviewPrCommentOutput(updated=False)

        github = _github_integration(ctx)
        if github is None:
            return EndPreviewPrCommentOutput(updated=False, failure="no_github_integration")

        output = task_run.output if isinstance(task_run.output, dict) else {}
        ref = _pull_request_ref(github, output.get("pr_url"))
        if ref is None:
            return EndPreviewPrCommentOutput(updated=False, failure="invalid_pr_url")

        try:
            response = github.api_request(
                "PATCH",
                f"/repos/{ref.owner}/{ref.repo}/issues/comments/{comment_id}",
                endpoint=_COMMENT_ENDPOINT,
                json_body={"body": _PREVIEW_ENDED_BODY},
            )
        except Exception:
            logger.warning("end_preview_pr_comment_request_failed", run_id=ctx.run_id, exc_info=True)
            emit_agent_log(ctx.run_id, "warn", "Could not mark the preview link on the pull request as ended.")
            return EndPreviewPrCommentOutput(updated=False, failure="github_unavailable")

        if response.status_code != 200:
            logger.warning(
                "end_preview_pr_comment_rejected",
                run_id=ctx.run_id,
                status_code=response.status_code,
            )
            return EndPreviewPrCommentOutput(updated=False, failure=f"status_{response.status_code}")

        return EndPreviewPrCommentOutput(updated=True)
