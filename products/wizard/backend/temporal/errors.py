from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError

from products.wizard.backend.facade.enums import WizardRunErrorCode
from products.wizard.backend.temporal.activities.errors import (
    WIZARD_REPOSITORY_ACCESS_ERROR_TYPE,
    WIZARD_WORKER_TIMEOUT_ERROR_TYPE,
)


class WizardTemporalError(Exception):
    pass


def wizard_run_error_code(error: ActivityError) -> WizardRunErrorCode:
    cause = error.cause
    if isinstance(cause, TimeoutError):
        return WizardRunErrorCode.TIMEOUT
    if isinstance(cause, ApplicationError) and cause.type == WIZARD_WORKER_TIMEOUT_ERROR_TYPE:
        return WizardRunErrorCode.TIMEOUT
    if isinstance(cause, ApplicationError) and cause.type == WIZARD_REPOSITORY_ACCESS_ERROR_TYPE:
        return WizardRunErrorCode.REPOSITORY_ACCESS_FAILED
    match error.activity_type:
        case "wizard_provision_worker":
            return WizardRunErrorCode.PROVISIONING_FAILED
        case "wizard_clone_repository":
            return WizardRunErrorCode.WORKSPACE_PREPARATION_FAILED
        case "wizard_create_run_artifacts":
            return WizardRunErrorCode.ARTIFACT_CREATION_FAILED
        case _:
            return WizardRunErrorCode.EXECUTION_FAILED
