import logging
from dataclasses import replace
from functools import partial
from uuid import UUID

from django.db import transaction as database_transaction

from posthog.models import Team, User

from products.tasks.backend.facade import repo_selection
from products.wizard.backend.facade.contracts import (
    CreateWizardRunInput,
    GitRepositoryWorkspace,
    ListWizardRunsInput,
    LocalFolderWorkspace,
    WizardRunCreationResult,
    WizardRunDTO,
    WizardRunPage,
)
from products.wizard.backend.facade.enums import (
    WizardRunEnvironment,
    WizardRunErrorCode,
    WizardRunStage,
    WizardRunStatus,
)
from products.wizard.backend.facade.errors import (
    IllegalStatusTransitionError,
    InvalidWorkspaceEnvironmentError,
    MissingGitHubIntegrationError,
    MissingWizardRunIdempotencyKeyError,
    RepositoryNotAccessibleError,
    WizardProgramEnvironmentNotSupportedError,
    WizardRunIdempotencyConflictError,
)
from products.wizard.backend.logic import registry as registry_service
from products.wizard.backend.logic.runs import (
    cancellation as cancellation_service,
    store,
)
from products.wizard.backend.logic.runs.admission import admit_cloud_run
from products.wizard.backend.logic.runs.fingerprints import create_run_request_fingerprint
from products.wizard.backend.logic.runs.queue import enqueue_dispatch
from products.wizard.backend.logic.runs.transitions import transition
from products.wizard.backend.logic.runs.validation import validate_git_repository
from products.wizard.backend.observability import service as run_observability

logger = logging.getLogger(__name__)


def create_run(params: CreateWizardRunInput) -> WizardRunDTO:
    return create_run_with_result(params).run


def create_run_with_result(params: CreateWizardRunInput) -> WizardRunCreationResult:
    match params.environment, params.workspace:
        case WizardRunEnvironment.LOCAL, LocalFolderWorkspace():
            pass
        case WizardRunEnvironment.CLOUD, GitRepositoryWorkspace():
            pass
        case _:
            raise InvalidWorkspaceEnvironmentError

    request_fingerprint: str | None = None
    if params.environment == WizardRunEnvironment.CLOUD:
        if params.idempotency_key is None:
            raise MissingWizardRunIdempotencyKeyError
        request_fingerprint = create_run_request_fingerprint(params)
        existing = store.get_run_by_idempotency_key(params.team_id, params.idempotency_key)
        if existing is not None:
            if store.get_request_fingerprint(params.team_id, existing.id) != request_fingerprint:
                raise WizardRunIdempotencyConflictError
            return WizardRunCreationResult(run=existing, created=False)

    user = User.objects.only("distinct_id").get(id=params.created_by_id)
    team = Team.objects.only("organization_id").get(id=params.team_id)

    program = registry_service.get_program(
        program_id=params.program_id,
        distinct_id=user.distinct_id,
        organization_id=str(team.organization_id),
    )

    if params.wizard_version is not None:
        program = replace(program, wizard_version=params.wizard_version)

    if params.environment not in program.supported_environments:
        raise WizardProgramEnvironmentNotSupportedError

    if isinstance(params.workspace, GitRepositoryWorkspace):
        validate_git_repository(params.workspace.repository)

        integration_id = repo_selection.resolve_team_github_integration_id(params.team_id)

        if integration_id is None:
            raise MissingGitHubIntegrationError

        if not repo_selection.repository_accessible_via_integration(
            params.team_id,
            integration_id,
            params.workspace.repository,
        ):
            raise RepositoryNotAccessibleError

    initial_status = (
        WizardRunStatus.RUNNING if params.environment == WizardRunEnvironment.LOCAL else WizardRunStatus.CREATED
    )

    with database_transaction.atomic():
        if params.environment == WizardRunEnvironment.CLOUD:
            admit_cloud_run(params.team_id, params.created_by_id, params.idempotency_key)
        result = store.create_run(
            team_id=params.team_id,
            created_by_id=params.created_by_id,
            environment=params.environment,
            workspace=params.workspace,
            program=program,
            status=initial_status,
            idempotency_key=params.idempotency_key,
            request_fingerprint=request_fingerprint,
        )

        if not result.created and store.get_request_fingerprint(params.team_id, result.run.id) != request_fingerprint:
            raise WizardRunIdempotencyConflictError

        if params.environment == WizardRunEnvironment.CLOUD and result.created:
            database_transaction.on_commit(
                partial(
                    _enqueue_cloud_run,
                    params.team_id,
                    result.run.id,
                ),
                robust=True,
            )

    if params.environment == WizardRunEnvironment.CLOUD:
        result = WizardRunCreationResult(run=store.get_run(params.team_id, result.run.id), created=result.created)
    if result.created:
        run_observability.run_created(result.run)
    return result


def _enqueue_cloud_run(team_id: int, run_id: UUID) -> None:
    try:
        enqueue_dispatch(team_id, run_id)
    except Exception:
        logger.exception(
            "wizard_run_dispatch_enqueue_failed",
            extra={"team_id": team_id, "run_id": str(run_id)},
        )


def get_run(team_id: int, run_id: UUID) -> WizardRunDTO:
    return store.get_run(team_id, run_id)


def list_runs(params: ListWizardRunsInput) -> WizardRunPage:
    return store.list_runs(params)


def start_run(team_id: int, run_id: UUID) -> WizardRunDTO:
    return transition_run(team_id, run_id, WizardRunStatus.RUNNING)


def complete_run(team_id: int, run_id: UUID) -> WizardRunDTO:
    return transition_run(team_id, run_id, WizardRunStatus.COMPLETED)


def fail_run(
    team_id: int,
    run_id: UUID,
    *,
    error_code: WizardRunErrorCode | None = None,
) -> WizardRunDTO:
    return transition_run(team_id, run_id, WizardRunStatus.FAILED, error_code=error_code)


def cancel_run(team_id: int, run_id: UUID) -> WizardRunDTO:
    return transition_run(team_id, run_id, WizardRunStatus.CANCELLED)


def cancel_cloud_run(team_id: int, run_id: UUID) -> WizardRunDTO:
    run = store.get_run(team_id, run_id)
    if run.environment != WizardRunEnvironment.CLOUD:
        raise InvalidWorkspaceEnvironmentError
    cancelled = transition_run(team_id, run_id, WizardRunStatus.CANCELLED)
    request_cloud_run_cancellation(team_id, run_id)
    return cancelled


def request_cloud_run_cancellation(team_id: int, run_id: UUID) -> None:
    store.mark_cancellation_requested(team_id, run_id)
    cancellation_service.deliver_cancellation(team_id, run_id)


def advance_run_stage(team_id: int, run_id: UUID, stage: WizardRunStage) -> WizardRunDTO:
    run = store.get_run(team_id, run_id)
    if run.status not in (WizardRunStatus.CREATED, WizardRunStatus.RUNNING):
        raise IllegalStatusTransitionError
    return store.set_run_stage(team_id, run_id, stage)


def transition_run(
    team_id: int,
    run_id: UUID,
    next_status: WizardRunStatus,
    *,
    error_code: WizardRunErrorCode | None = None,
) -> WizardRunDTO:
    with database_transaction.atomic():
        run = store.get_run(team_id, run_id, lock=True)
        previous = run
        next_status = transition(run.status, next_status, error_code=error_code)
        run = store.set_run_status(team_id, run_id, next_status, error_code)

    run_observability.run_transitioned(previous, run)
    return run
