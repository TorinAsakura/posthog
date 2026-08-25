import posthoganalytics

from products.wizard.backend.facade.contracts import WizardProgram
from products.wizard.backend.facade.errors import WizardProgramNotAvailableError
from products.wizard.backend.logic.registry.config import FALLBACK_REGISTRY, REGISTRY_FEATURE_FLAG
from products.wizard.backend.logic.registry.parser import parse_registry_payload


def get_registry(*, distinct_id: str, organization_id: str) -> tuple[WizardProgram, ...]:
    payload = posthoganalytics.get_feature_flag_payload(
        REGISTRY_FEATURE_FLAG,
        distinct_id=distinct_id,
        groups={"organization": organization_id},
        group_properties={"organization": {"id": organization_id}},
        only_evaluate_locally=False,
        send_feature_flag_events=False,
    )

    try:
        registry = parse_registry_payload(payload)
    except ValueError:
        return FALLBACK_REGISTRY.programs
    return registry.programs


def get_program(*, program_id: str, distinct_id: str, organization_id: str) -> WizardProgram:
    programs = get_registry(distinct_id=distinct_id, organization_id=organization_id)
    program = next((program for program in programs if program.id == program_id), None)
    if program is None:
        raise WizardProgramNotAvailableError
    return program
