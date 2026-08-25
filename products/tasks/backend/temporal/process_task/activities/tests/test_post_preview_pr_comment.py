import pytest
from unittest.mock import MagicMock

from django.test import override_settings

from posthog.models.github_integration_base import PullRequestRef

from products.tasks.backend.constants import DEV_STACK_PREVIEW_STATE_KEY
from products.tasks.backend.models import TaskRun
from products.tasks.backend.temporal.process_task.activities.post_preview_pr_comment import (
    PREVIEW_COMMENT_MARKER,
    EndPreviewPrCommentInput,
    PostPreviewPrCommentInput,
    end_preview_pr_comment,
    post_preview_pr_comment,
)

MODULE = "products.tasks.backend.temporal.process_task.activities.post_preview_pr_comment"
PR_URL = "https://github.com/org/repo/pull/7"
SITE_URL = "https://us.posthog.example"


def _github(mocker, *, status_code=201, payload=None, side_effect=None, existing_comments=None):
    integration = MagicMock()
    write_response = MagicMock(status_code=status_code)
    write_response.json.return_value = payload if payload is not None else {"id": 4242}
    list_response = MagicMock(status_code=200)
    list_response.json.return_value = existing_comments if existing_comments is not None else []

    def _api_request(method, path, **kwargs):
        return list_response if method == "GET" else write_response

    integration.api_request.side_effect = side_effect if side_effect is not None else _api_request
    integration.parse_pull_request_url.side_effect = lambda url: _ref(url)
    mocker.patch(f"{MODULE}.get_github_integration", return_value=integration)
    return integration


def _calls(github, method):
    return [call for call in github.api_request.call_args_list if call.args[0] == method]


def _ref(url):
    if not url or not url.startswith("https://github.com/"):
        return None
    owner, repo, _, number = url.removeprefix("https://github.com/").split("/")[:4]
    return PullRequestRef(owner=owner, repo=repo, number=int(number))


def _stamp_preview(task_run, **extra):
    TaskRun.update_state_atomic(
        task_run.id, updates={DEV_STACK_PREVIEW_STATE_KEY: {"port": 8010, "ready_at": "2026-08-25T10:00:00Z", **extra}}
    )


def _post(context, pr_url=PR_URL):
    return post_preview_pr_comment(PostPreviewPrCommentInput(context=context, pr_url=pr_url))


def _end(context):
    return end_preview_pr_comment(EndPreviewPrCommentInput(context=context))


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_no_preview_in_state_posts_nothing(task_context, test_task_run, mocker):
    github = _github(mocker)

    result = _post(task_context)

    assert result.posted is False
    github.api_request.assert_not_called()


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_preview_posts_one_comment_with_the_posthog_url_and_records_its_id(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run)
    github = _github(mocker)

    result = _post(task_context)

    assert result.posted is True
    assert result.comment_id == 4242
    posts = _calls(github, "POST")
    assert len(posts) == 1
    assert posts[0].args[1] == "/repos/org/repo/issues/7/comments"
    body = posts[0].kwargs["json_body"]["body"]
    expected_url = (
        f"{SITE_URL}/api/projects/{task_context.team_id}/tasks/{task_context.task_id}"
        f"/runs/{task_context.run_id}/preview/"
    )
    assert expected_url in body
    assert body.startswith(PREVIEW_COMMENT_MARKER)
    assert "modal.host" not in body
    assert "_modal_connect_token" not in body

    test_task_run.refresh_from_db()
    assert test_task_run.state[DEV_STACK_PREVIEW_STATE_KEY]["pr_comment_id"] == 4242
    assert test_task_run.state[DEV_STACK_PREVIEW_STATE_KEY]["port"] == 8010


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_recorded_comment_id_stops_a_second_post(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run, pr_comment_id=4242)
    github = _github(mocker)

    result = _post(task_context)

    assert result.posted is False
    assert result.comment_id == 4242
    github.api_request.assert_not_called()


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_an_existing_preview_comment_is_reused_instead_of_posting_a_second_one(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run)
    github = _github(
        mocker,
        existing_comments=[
            {"id": 111, "body": "unrelated review comment"},
            {"id": 4242, "body": f"{PREVIEW_COMMENT_MARKER}\n**PostHog preview**"},
        ],
    )

    result = _post(task_context)

    assert result.posted is False
    assert result.comment_id == 4242
    assert _calls(github, "POST") == []
    test_task_run.refresh_from_db()
    assert test_task_run.state[DEV_STACK_PREVIEW_STATE_KEY]["pr_comment_id"] == 4242


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_github_failure_returns_a_result_instead_of_raising(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run)
    github = _github(mocker, side_effect=RuntimeError("github is down"))

    result = _post(task_context)

    assert result.posted is False
    assert result.failure == "github_unavailable"
    assert len(_calls(github, "POST")) == 1
    test_task_run.refresh_from_db()
    assert "pr_comment_id" not in test_task_run.state[DEV_STACK_PREVIEW_STATE_KEY]


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_teardown_edits_the_recorded_comment(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run, pr_comment_id=4242)
    TaskRun.update_output_atomic(test_task_run.id, updates={"pr_url": PR_URL})
    github = _github(mocker, status_code=200)

    result = _end(task_context)

    assert result.updated is True
    method, path = github.api_request.call_args.args
    assert (method, path) == ("PATCH", "/repos/org/repo/issues/comments/4242")
    body = github.api_request.call_args.kwargs["json_body"]["body"]
    assert "This preview has ended. Rerun the task to start a new one." in body


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_teardown_without_a_recorded_comment_touches_nothing(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run)
    github = _github(mocker, status_code=200)

    result = _end(task_context)

    assert result.updated is False
    github.api_request.assert_not_called()


@pytest.mark.django_db
@override_settings(SITE_URL=SITE_URL)
def test_teardown_github_failure_returns_a_result_instead_of_raising(task_context, test_task_run, mocker):
    _stamp_preview(test_task_run, pr_comment_id=4242)
    TaskRun.update_output_atomic(test_task_run.id, updates={"pr_url": PR_URL})
    _github(mocker, side_effect=RuntimeError("github is down"))

    result = _end(task_context)

    assert result.updated is False
    assert result.failure == "github_unavailable"
