from datetime import timedelta

import pytest
from unittest.mock import patch

from django.utils import timezone

from products.wizard.backend.facade.enums import (
    WizardRunDispatchStatus,
    WizardRunEnvironment,
    WizardRunErrorCode,
    WizardRunStatus,
    WizardWorkerCleanupStatus,
    WizardWorkspaceType,
)
from products.wizard.backend.logic.programs import program_to_mapping
from products.wizard.backend.logic.registry.config import POSTHOG_INTEGRATION_PROGRAM
from products.wizard.backend.logic.runs import lifecycle, reconciliation
from products.wizard.backend.models import WizardRun, WizardWorker
from products.wizard.backend.temporal.errors import WizardTemporalError


def _create_cloud_run(team_id: int, user_id: int, **values: object) -> WizardRun:
    defaults = {
        "team_id": team_id,
        "created_by_id": user_id,
        "environment": WizardRunEnvironment.CLOUD.value,
        "workspace_type": WizardWorkspaceType.GIT_REPOSITORY.value,
        "workspace": {"repository": "posthog/posthog"},
        "program": program_to_mapping(POSTHOG_INTEGRATION_PROGRAM),
        "status": WizardRunStatus.CREATED.value,
        "dispatch_status": WizardRunDispatchStatus.PENDING.value,
        "deadline_at": timezone.now() + timedelta(hours=1),
    }
    defaults.update(values)
    return WizardRun.objects.for_team(team_id).create(**defaults)


@pytest.mark.django_db
def test_reconciliation_redispatches_pending_run(team, user) -> None:
    run = _create_cloud_run(team.id, user.id)

    with patch(
        "products.wizard.backend.logic.runs.reconciliation.dispatch_wizard_run_to_temporal_worker"
    ) as dispatch_wizard_run:
        result = reconciliation.reconcile_pending_dispatches()

    assert result == 1
    dispatch_wizard_run.assert_called_once_with(team.id, run.id)


@pytest.mark.django_db
def test_cloud_cancellation_survives_temporal_failure(team, user) -> None:
    run = _create_cloud_run(
        team.id,
        user.id,
        status=WizardRunStatus.RUNNING.value,
        dispatch_status=WizardRunDispatchStatus.DISPATCHED.value,
        workflow_id="wizard-run-id",
    )

    with patch(
        "products.wizard.backend.logic.runs.cancellation.temporal_client.cancel_wizard_run_workflow",
        side_effect=WizardTemporalError,
    ):
        cancelled = lifecycle.cancel_run(team.id, run.id)

    record = WizardRun.objects.for_team(team.id).get(id=run.id)
    assert cancelled.status == WizardRunStatus.CANCELLED
    assert record.cancellation_requested_at is not None
    assert record.cancellation_dispatched_at is None


@pytest.mark.django_db
def test_reconciliation_retries_pending_cancellation(team, user) -> None:
    run = _create_cloud_run(
        team.id,
        user.id,
        status=WizardRunStatus.CANCELLED.value,
        dispatch_status=WizardRunDispatchStatus.DISPATCHED.value,
        workflow_id="wizard-run-id",
        cancellation_requested_at=timezone.now(),
    )

    with patch("products.wizard.backend.logic.runs.cancellation.temporal_client.cancel_wizard_run_workflow") as cancel:
        result = reconciliation.reconcile_pending_cancellations()

    assert result == 1
    cancel.assert_called_once_with(run.id)
    run.refresh_from_db()
    assert run.cancellation_dispatched_at is not None


@pytest.mark.django_db
def test_reconciliation_fails_expired_run(team, user) -> None:
    run = _create_cloud_run(team.id, user.id, deadline_at=timezone.now() - timedelta(seconds=1))

    result = reconciliation.reconcile_expired_runs()

    assert result == 1
    run.refresh_from_db()
    assert run.status == WizardRunStatus.FAILED.value
    assert run.error_code == WizardRunErrorCode.TIMEOUT.value


@pytest.mark.django_db
def test_reconciliation_destroys_pending_worker(team, user) -> None:
    run = _create_cloud_run(team.id, user.id, status=WizardRunStatus.COMPLETED.value)
    worker = WizardWorker.objects.for_team(team.id).create(
        team_id=team.id,
        run=run,
        sandbox_id="sandbox-id",
        cleanup_status=WizardWorkerCleanupStatus.PENDING.value,
    )

    with (
        patch(
            "products.wizard.backend.logic.runs.worker_lifecycle.cloud_worker.measure_worker_usage",
            return_value=None,
        ),
        patch("products.wizard.backend.logic.runs.worker_lifecycle.cloud_worker.destroy_worker") as destroy,
    ):
        result = reconciliation.reconcile_pending_worker_cleanup()

    assert result == 1
    destroy.assert_called_once_with("sandbox-id")
    worker.refresh_from_db()
    assert worker.cleanup_status == WizardWorkerCleanupStatus.CLEANED.value
