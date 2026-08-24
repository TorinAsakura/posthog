from uuid import UUID

from celery import current_app as celery_app

from products.wizard.backend.tasks.config import DISPATCH_WIZARD_RUN_TASK


def enqueue_dispatch(team_id: int, run_id: UUID) -> None:
    # review: why are using a celery task to dispatch runs? Why not use temporal?
    celery_app.signature(DISPATCH_WIZARD_RUN_TASK, args=[team_id, str(run_id)]).apply_async()
