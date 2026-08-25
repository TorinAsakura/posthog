import logging

from django.utils import timezone

from posthog.dataclasses import frozen

from products.wizard.backend.facade.enums import (
    WizardRunDispatchStatus,
    WizardRunErrorCode,
    WizardRunStatus,
    WizardWorkerCleanupStatus,
)
from products.wizard.backend.facade.errors import IllegalStatusTransitionError, WizardRunNotFoundError
from products.wizard.backend.logic.runs import cancellation, lifecycle, worker_lifecycle
from products.wizard.backend.logic.runs.config import RECONCILIATION_BATCH_SIZE
from products.wizard.backend.logic.runs.dispatch import dispatch_created_cloud_wizard_run_to_temporal_worker
from products.wizard.backend.logic.runs.errors import WizardRunDispatchError, WizardWorkerCleanupError
from products.wizard.backend.models import WizardRun, WizardWorker

logger = logging.getLogger(__name__)


@frozen
class ReconciliationSummary:
    scanned: int
    reconciled: int
    failed: int
    batch_limit_reached: bool


def _summary(scanned: int, reconciled: int) -> ReconciliationSummary:
    return ReconciliationSummary(
        scanned=scanned,
        reconciled=reconciled,
        failed=scanned - reconciled,
        batch_limit_reached=scanned == RECONCILIATION_BATCH_SIZE,
    )


def reconcile_pending_dispatches() -> ReconciliationSummary:
    pending = list(
        WizardRun.objects.unscoped()
        .filter(
            status=WizardRunStatus.CREATED.value,
            dispatch_status=WizardRunDispatchStatus.PENDING.value,
        )
        .values_list("team_id", "id")[:RECONCILIATION_BATCH_SIZE]
    )

    reconciled = 0
    for team_id, run_id in pending:
        try:
            dispatch_created_cloud_wizard_run_to_temporal_worker(team_id, run_id)
        except (WizardRunDispatchError, WizardRunNotFoundError):
            logger.exception("wizard_run_redispatch_failed", extra={"team_id": team_id, "run_id": str(run_id)})
            continue
        reconciled += 1

    return _summary(len(pending), reconciled)


def reconcile_pending_cancellations() -> ReconciliationSummary:
    pending = list(
        WizardRun.objects.unscoped()
        .filter(
            status__in=(WizardRunStatus.CANCELLED.value, WizardRunStatus.FAILED.value),
            cancellation_requested_at__isnull=False,
            cancellation_dispatched_at__isnull=True,
        )
        .values_list("team_id", "id")[:RECONCILIATION_BATCH_SIZE]
    )

    reconciled = sum(cancellation.dispatch_cancellation(team_id, run_id) for team_id, run_id in pending)

    return _summary(len(pending), reconciled)


def reconcile_expired_runs() -> ReconciliationSummary:
    expired = list(
        WizardRun.objects.unscoped()
        .filter(
            status__in=(WizardRunStatus.CREATED.value, WizardRunStatus.RUNNING.value),
            deadline_at__lte=timezone.now(),
        )
        .values_list("team_id", "id", "workflow_id")[:RECONCILIATION_BATCH_SIZE]
    )

    reconciled = 0
    for team_id, run_id, workflow_id in expired:
        try:
            lifecycle.fail_run(team_id, run_id, error_code=WizardRunErrorCode.TIMEOUT)
        except (IllegalStatusTransitionError, WizardRunNotFoundError):
            logger.exception("wizard_run_expiration_failed", extra={"team_id": team_id, "run_id": str(run_id)})
            continue
        if workflow_id is not None:
            lifecycle.request_cloud_run_cancellation(team_id, run_id)
        reconciled += 1

    return _summary(len(expired), reconciled)


def reconcile_pending_worker_cleanup() -> ReconciliationSummary:
    pending = list(
        WizardWorker.objects.unscoped()
        .filter(
            cleanup_status=WizardWorkerCleanupStatus.PENDING.value,
            sandbox_id__isnull=False,
        )
        .values_list("team_id", "run_id", "sandbox_id")[:RECONCILIATION_BATCH_SIZE]
    )

    reconciled = 0
    for team_id, run_id, sandbox_id in pending:
        if sandbox_id is None:
            continue
        try:
            worker_lifecycle.cleanup_worker(team_id, run_id, sandbox_id)
        except WizardWorkerCleanupError:
            logger.exception("wizard_worker_reconciliation_failed", extra={"team_id": team_id, "run_id": str(run_id)})
            continue
        reconciled += 1

    return _summary(len(pending), reconciled)
