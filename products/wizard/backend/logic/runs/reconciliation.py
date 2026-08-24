import logging

from django.utils import timezone

from products.wizard.backend.facade.enums import (
    WizardRunDispatchStatus,
    WizardRunErrorCode,
    WizardRunStatus,
    WizardWorkerCleanupStatus,
)
from products.wizard.backend.logic.runs import cancellation, lifecycle, worker_lifecycle
from products.wizard.backend.logic.runs.config import RECONCILIATION_BATCH_SIZE
from products.wizard.backend.logic.runs.queue import enqueue_dispatch
from products.wizard.backend.models import WizardRun, WizardWorker

logger = logging.getLogger(__name__)


def reconcile_pending_dispatches() -> int:
    pending = (
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
            enqueue_dispatch(team_id, run_id)
        except Exception:
            logger.exception("wizard_run_redispatch_failed", extra={"team_id": team_id, "run_id": str(run_id)})
            continue
        reconciled += 1
    return reconciled


def reconcile_pending_cancellations() -> int:
    pending = (
        WizardRun.objects.unscoped()
        .filter(
            status__in=(WizardRunStatus.CANCELLED.value, WizardRunStatus.FAILED.value),
            cancellation_requested_at__isnull=False,
            cancellation_dispatched_at__isnull=True,
        )
        .values_list("team_id", "id")[:RECONCILIATION_BATCH_SIZE]
    )
    return sum(cancellation.deliver_cancellation(team_id, run_id) for team_id, run_id in pending)


def reconcile_expired_runs() -> int:
    expired = (
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
        except Exception:
            logger.exception("wizard_run_expiration_failed", extra={"team_id": team_id, "run_id": str(run_id)})
            continue
        if workflow_id is not None:
            lifecycle.request_cloud_run_cancellation(team_id, run_id)
        reconciled += 1
    return reconciled


def reconcile_pending_worker_cleanup() -> int:
    pending = (
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
        except Exception:
            logger.exception("wizard_worker_reconciliation_failed", extra={"team_id": team_id, "run_id": str(run_id)})
            continue
        reconciled += 1
    return reconciled
