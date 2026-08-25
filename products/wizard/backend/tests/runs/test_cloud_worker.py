from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import MagicMock, patch

from parameterized import parameterized

from products.tasks.backend.facade.repository import RepositoryPullRequest
from products.tasks.backend.facade.sandbox import SandboxNotFoundError
from products.wizard.backend.logic.workers.commands import wizard_handoff_output_path
from products.wizard.backend.logic.workers.service import (
    GitRepositoryCloneRequest,
    GitRepositoryHandoffRequest,
    WizardExecutionRequest,
    WizardWorkerExecutionError,
    WizardWorkerProvisionRequest,
    WizardWorkerResult,
    WizardWorkerTimeoutError,
    clone_repository,
    create_git_repository_handoff,
    destroy_worker,
    execute_wizard,
    provision_wizard_worker,
)


def _execution_result(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
@patch("products.wizard.backend.logic.workers.service.create_wizard_oauth_access_token_for_user")
@patch("products.wizard.backend.logic.workers.service.User.objects.get")
def test_provision_worker_configures_wizard_environment(
    get_user: MagicMock,
    create_wizard_token: MagicMock,
    get_sandbox_class: MagicMock,
) -> None:
    request = WizardWorkerProvisionRequest(team_id=7, created_by_id=13, run_id=uuid4())
    create_wizard_token.return_value = "wizard-secret"
    get_sandbox_class.return_value.create.return_value.id = "worker-id"

    provisioning = provision_wizard_worker(request)

    assert provisioning.sandbox_id == "worker-id"
    assert provisioning.resource_usage.cpu_cores == 2
    assert provisioning.resource_usage.memory_gb == 4
    assert provisioning.resource_usage.disk_size_gb == 16
    assert provisioning.resource_usage.ttl_seconds == 75 * 60
    get_user.assert_called_once_with(id=request.created_by_id)
    config = get_sandbox_class.return_value.create.call_args.args[0]
    assert "GITHUB_TOKEN" not in config.environment_variables
    assert config.environment_variables["POSTHOG_WIZARD_API_KEY"] == "wizard-secret"
    assert "POSTHOG_WIZARD_RUN_ID" not in config.environment_variables
    assert config.environment_variables["POSTHOG_HANDOFF_OUTPUT_PATH"] == wizard_handoff_output_path(request.run_id)
    assert config.ttl_seconds == 75 * 60


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
@patch("products.wizard.backend.logic.workers.service.get_github_token")
def test_clone_repository_uses_integration_token(
    get_github_token: MagicMock,
    get_sandbox_class: MagicMock,
) -> None:
    request = GitRepositoryCloneRequest(
        sandbox_id="worker-id",
        github_integration_id=17,
        repository="PostHog/PostHog",
    )
    get_github_token.return_value = "github-secret"
    sandbox = get_sandbox_class.return_value.get_by_id.return_value
    sandbox.clone_repository.return_value = _execution_result()

    root_path = clone_repository(request)

    assert root_path == "/tmp/workspace/repos/posthog/posthog"
    get_sandbox_class.return_value.get_by_id.assert_called_once_with(request.sandbox_id)
    sandbox.clone_repository.assert_called_once_with(request.repository, github_token="github-secret", shallow=True)


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
@patch("products.wizard.backend.logic.workers.service.get_github_token", return_value="github-secret")
def test_clone_repository_rejects_clone_failure(
    _get_github_token: MagicMock,
    get_sandbox_class: MagicMock,
) -> None:
    request = GitRepositoryCloneRequest(
        sandbox_id="worker-id",
        github_integration_id=17,
        repository="PostHog/PostHog",
    )
    sandbox = get_sandbox_class.return_value.get_by_id.return_value
    sandbox.clone_repository.return_value = _execution_result(stderr="not found", exit_code=128)

    with pytest.raises(WizardWorkerExecutionError):
        clone_repository(request)


@parameterized.expand(
    (
        ("default", (), "npx --yes @posthog/wizard@2.60.0 --headless-DONOTUSE-EXPERIMENTAL"),
        (
            "nested",
            ("audit", "web-analytics"),
            "npx --yes @posthog/wizard@2.60.0 audit web-analytics --headless-DONOTUSE-EXPERIMENTAL",
        ),
    )
)
@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_execute_wizard_uses_selected_program(
    _name: str,
    program_command: tuple[str, ...],
    expected_invocation: str,
    get_sandbox_class: MagicMock,
) -> None:
    request = WizardExecutionRequest(
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        team_id=7,
        wizard_version="2.60.0",
        program_command=program_command,
    )
    sandbox = get_sandbox_class.return_value.get_by_id.return_value
    sandbox.execute.return_value = _execution_result()

    execute_wizard(request)

    get_sandbox_class.return_value.get_by_id.assert_called_once_with(request.sandbox_id)
    command = sandbox.execute.call_args.args[0]
    assert command.startswith(f"cd {request.workspace_path} &&")
    assert expected_invocation in command
    assert "wizard-secret" not in command


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_execute_wizard_rejects_program_options(get_sandbox_class: MagicMock) -> None:
    request = WizardExecutionRequest(
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        team_id=7,
        wizard_version="2.60.0",
        program_command=("--base-url",),
    )

    with pytest.raises(ValueError, match="Invalid Wizard program command"):
        execute_wizard(request)

    get_sandbox_class.return_value.get_by_id.return_value.execute.assert_not_called()


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_execute_wizard_rejects_mutable_version(get_sandbox_class: MagicMock) -> None:
    request = WizardExecutionRequest(
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        team_id=7,
        wizard_version="^2.60.0",
        program_command=(),
    )

    with pytest.raises(ValueError, match="Invalid Wizard version"):
        execute_wizard(request)

    get_sandbox_class.return_value.get_by_id.return_value.execute.assert_not_called()


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_execute_wizard_maps_command_timeout(get_sandbox_class: MagicMock) -> None:
    request = WizardExecutionRequest(
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        team_id=7,
        wizard_version="2.60.0",
        program_command=(),
    )
    get_sandbox_class.return_value.get_by_id.return_value.execute.return_value = _execution_result(exit_code=124)

    with pytest.raises(WizardWorkerTimeoutError):
        execute_wizard(request)


@patch("products.wizard.backend.logic.workers.service.repository_facade.create_pull_request")
@patch("products.wizard.backend.logic.workers.service.repository_facade.create_signed_commit")
@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_git_repository_handoff_captures_diff_and_publishes_pull_request(
    get_sandbox_class: MagicMock,
    create_signed_commit: MagicMock,
    create_pull_request: MagicMock,
) -> None:
    request = GitRepositoryHandoffRequest(
        team_id=7,
        run_id=uuid4(),
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        github_integration_id=17,
        repository="PostHog/PostHog",
    )
    sandbox = get_sandbox_class.return_value.get_by_id.return_value
    sandbox.execute.side_effect = (
        _execution_result(stdout="diff --git a/a b/a\n"),
        _execution_result(stdout="# Setup report\n\nAll done.\n"),
    )
    branch = f"posthog/wizard-{request.run_id.hex[:12]}"
    pull_request = RepositoryPullRequest(
        repository=request.repository,
        number=123,
        url="https://github.com/posthog/posthog/pull/123",
        head_branch=branch,
        base_branch="master",
    )
    create_pull_request.return_value = pull_request

    result = create_git_repository_handoff(request)

    assert result == WizardWorkerResult(diff=b"diff --git a/a b/a\n", pull_request=pull_request)
    assert "git add -N --all" in sandbox.execute.call_args_list[0].args[0]
    assert wizard_handoff_output_path(request.run_id) in sandbox.execute.call_args_list[1].args[0]
    create_signed_commit.assert_called_once_with(
        sandbox,
        repository=request.repository,
        branch=branch,
        message="Set up PostHog",
    )
    create_pull_request.assert_called_once_with(
        team_id=request.team_id,
        integration_id=request.github_integration_id,
        repository=request.repository,
        head_branch=branch,
        title="Set up PostHog",
        body="# Setup report\n\nAll done.",
        source="wizard",
    )


@pytest.mark.parametrize(
    "handoff_result",
    (
        _execution_result(exit_code=1),
        _execution_result(stdout="  \n"),
    ),
)
@patch("products.wizard.backend.logic.workers.service.repository_facade.create_pull_request")
@patch("products.wizard.backend.logic.workers.service.repository_facade.create_signed_commit")
@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_git_repository_handoff_uses_generic_body_when_handoff_is_unavailable(
    get_sandbox_class: MagicMock,
    create_signed_commit: MagicMock,
    create_pull_request: MagicMock,
    handoff_result: SimpleNamespace,
) -> None:
    request = GitRepositoryHandoffRequest(
        team_id=7,
        run_id=uuid4(),
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        github_integration_id=17,
        repository="PostHog/PostHog",
    )
    sandbox = get_sandbox_class.return_value.get_by_id.return_value
    sandbox.execute.side_effect = (_execution_result(stdout="diff --git a/a b/a\n"), handoff_result)

    create_git_repository_handoff(request)

    assert create_pull_request.call_args.kwargs["body"] == (
        "This pull request contains changes created by Wizard, PostHog's setup agent."
    )


@patch("products.wizard.backend.logic.workers.service.repository_facade.create_pull_request")
@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_git_repository_handoff_skips_publish_without_changes(
    get_sandbox_class: MagicMock,
    create_pull_request: MagicMock,
) -> None:
    request = GitRepositoryHandoffRequest(
        team_id=7,
        run_id=uuid4(),
        sandbox_id="worker-id",
        workspace_path="/tmp/workspace/repos/posthog/posthog",
        github_integration_id=17,
        repository="PostHog/PostHog",
    )
    get_sandbox_class.return_value.get_by_id.return_value.execute.return_value = _execution_result()

    result = create_git_repository_handoff(request)

    assert result == WizardWorkerResult(diff=b"", pull_request=None)
    get_sandbox_class.return_value.get_by_id.return_value.execute.assert_called_once()
    create_pull_request.assert_not_called()


@patch("products.wizard.backend.logic.workers.service.get_sandbox_class")
def test_destroy_worker_accepts_already_destroyed_sandbox(get_sandbox_class: MagicMock) -> None:
    get_sandbox_class.return_value.get_by_id.side_effect = SandboxNotFoundError(
        "Worker not found.",
        {"sandbox_id": "worker-id"},
        RuntimeError("not found"),
    )

    destroy_worker("worker-id")
