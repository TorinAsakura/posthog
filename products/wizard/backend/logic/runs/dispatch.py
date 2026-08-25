from uuid import UUID

from products.wizard.backend.facade.enums import WizardRunEnvironment, WizardRunStatus
from products.wizard.backend.logic.runs import store
from products.wizard.backend.logic.runs.errors import WizardRunDispatchError
from products.wizard.backend.observability.contracts import WizardRunDispatchOutcome
from products.wizard.backend.observability.service import wizard_observability
from products.wizard.backend.temporal import client as temporal_client
from products.wizard.backend.temporal.constants import wizard_run_workflow_id
from products.wizard.backend.temporal.contracts import WizardRunActivityInput
from products.wizard.backend.temporal.errors import WizardTemporalError


def dispatch_wizard_run_to_temporal_worker(team_id: int, run_id: UUID) -> None:
    # review: i understand this is done to make sure the run exists, but the logic should be explicit, and implicit in the function call
    run = store.get_run(team_id, run_id)

    if run.environment != WizardRunEnvironment.CLOUD or run.status != WizardRunStatus.CREATED:
        return

    try:
        temporal_client.start_wizard_run_workflow(WizardRunActivityInput(team_id=team_id, run_id=run_id))
    except WizardTemporalError as error:
        store.mark_dispatch_failed(team_id, run_id)
        wizard_observability.dispatch_finished(run, WizardRunDispatchOutcome.FAILED)
        raise WizardRunDispatchError from error

    store.mark_dispatch_succeeded(team_id, run_id, wizard_run_workflow_id(run_id))
    wizard_observability.dispatch_finished(run, WizardRunDispatchOutcome.SUCCEEDED)
