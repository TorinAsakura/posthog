from celery import shared_task
from structlog import get_logger

from posthog.models.integration import Integration
from posthog.scoping_audit import skip_team_scope_audit
from posthog.tasks.utils import CeleryQueue

from products.workflows.backend.providers.ses import SESProvider
from products.workflows.backend.services.ses_tenant_state import sync_ses_tenant_state

logger = get_logger(__name__)

# SES does not reflect a tenant change in its read APIs the moment the event fires, and the sync
# deliberately trusts the API over the payload. A read that lands too early sees the old state and
# reports no change, which is indistinguishable from a no-op, so a change can be missed entirely
# until the daily sweep. Re-read a couple of times before giving up.
SYNC_INITIAL_DELAY_SECONDS = 15
SYNC_ATTEMPTS = 3
SYNC_RETRY_DELAY_SECONDS = 45


@shared_task(
    ignore_result=True,
    queue=CeleryQueue.DEFAULT.value,
    # The sync hits the SES API; transient throttling/outages should retry with backoff instead
    # of dropping the event (the daily sweep would catch it, but a day late).
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=600,
    max_retries=5,
    retry_jitter=True,
)
def sync_ses_tenant_state_task(team_id: int, attempt: int = 1) -> None:
    """Webhook-triggered: an EventBridge event said this team's tenant changed, fetch and apply."""
    if sync_ses_tenant_state(team_id):
        return
    if attempt >= SYNC_ATTEMPTS:
        logger.info("SES tenant state still unchanged, giving up", team_id=team_id, attempts=attempt)
        return
    logger.info("SES tenant state unchanged, re-reading", team_id=team_id, attempt=attempt)
    sync_ses_tenant_state_task.apply_async((team_id, attempt + 1), countdown=SYNC_RETRY_DELAY_SECONDS)


@shared_task(ignore_result=True, queue=CeleryQueue.LONG_RUNNING.value)
@skip_team_scope_audit
def reconcile_ses_tenant_states() -> None:
    """
    Periodic backstop: EventBridge delivery is best-effort, so sweep every team that has an SES
    tenant (i.e. a verified email integration) and apply the authoritative state. The transition
    logic dedupes against stored state, so overlap with webhook-triggered syncs is harmless.
    """
    team_ids = (
        Integration.objects.filter(kind="email", config__provider="ses")
        .values_list("team_id", flat=True)
        .distinct()
        .order_by("team_id")
    )
    # One provider for the whole sweep: a fresh SESProvider per team would rebuild its boto3
    # clients (and re-resolve credentials) thousands of times.
    provider = SESProvider()
    synced = 0
    failed = 0
    for team_id in team_ids.iterator(chunk_size=500):
        try:
            sync_ses_tenant_state(team_id, provider=provider, verify_team=False)
            synced += 1
        except Exception:
            failed += 1
            logger.exception("SES tenant reconciliation failed for team", team_id=team_id)
    logger.info("SES tenant reconciliation finished", synced=synced, failed=failed)
