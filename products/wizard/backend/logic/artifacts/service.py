from hashlib import sha256
from uuid import UUID

from posthog.storage import object_storage

from products.wizard.backend.facade.contracts import (
    CreatePullRequestArtifactInput,
    WizardRunArtifactDTO,
    WizardRunGitDiffArtifactDTO,
    WizardRunPullRequestArtifactDTO,
)
from products.wizard.backend.logic.artifacts import store
from products.wizard.backend.logic.artifacts.config import GIT_DIFF_CONTENT_TYPE, MAX_GIT_DIFF_BYTES
from products.wizard.backend.logic.runs import store as run_store
from products.wizard.backend.observability import service as run_observability


def create_git_diff_artifact(team_id: int, run_id: UUID, content: bytes) -> WizardRunGitDiffArtifactDTO | None:
    if not content:
        return None

    run = run_store.get_run(team_id, run_id)
    if len(content) > MAX_GIT_DIFF_BYTES:
        run_observability.git_diff_omitted(run, len(content))
        return None
    storage_path = _git_diff_storage_path(team_id, run_id)
    object_storage.write(storage_path, content, extras={"ContentType": GIT_DIFF_CONTENT_TYPE})
    return store.upsert_git_diff(
        team_id=team_id,
        run_id=run.id,
        storage_path=storage_path,
        size_bytes=len(content),
        content_hash=sha256(content).hexdigest(),
    )


def create_pull_request_artifact(params: CreatePullRequestArtifactInput) -> WizardRunPullRequestArtifactDTO:
    run = run_store.get_run(params.team_id, params.run_id)
    artifact = store.upsert_pull_request(
        team_id=params.team_id,
        run_id=run.id,
        url=params.url,
        number=params.number,
        repository=params.repository,
        head_branch=params.head_branch,
        base_branch=params.base_branch,
    )
    run_observability.pull_request_created(run, artifact)
    return artifact


def list_run_artifacts(team_id: int, run_id: UUID) -> list[WizardRunArtifactDTO]:
    # review: why do we need this call? I understand we need to validate if the run exists. But then that logic should be more explicit
    run = run_store.get_run(team_id, run_id)
    return store.list_artifacts(team_id, run.id)


def _git_diff_storage_path(team_id: int, run_id: UUID) -> str:
    return f"wizard/runs/team_{team_id}/run_{run_id}/artifacts/git.diff"
