import logging
from uuid import UUID

from products.wizard.backend.logic.runs import (
    worker as cloud_worker,
    worker_store,
)

logger = logging.getLogger(__name__)


def cleanup_worker(team_id: int, run_id: UUID, sandbox_id: str) -> None:
    worker_store.mark_cleanup_pending(team_id, run_id)

    usage = cloud_worker.measure_worker_usage(sandbox_id)

    if usage is not None:
        try:
            worker_store.record_usage(team_id, run_id, usage)
        except Exception:
            logger.exception(
                "wizard_worker_usage_recording_failed",
                extra={"team_id": team_id, "run_id": str(run_id), "sandbox_id": sandbox_id},
            )

    try:
        cloud_worker.destroy_worker(sandbox_id)
    except Exception:
        try:
            worker_store.mark_cleanup_failed(team_id, run_id)
        except Exception:
            logger.exception(
                "wizard_worker_cleanup_failure_recording_failed",
                extra={"team_id": team_id, "run_id": str(run_id), "sandbox_id": sandbox_id},
            )
        raise

    worker_store.mark_cleaned(team_id, run_id)
