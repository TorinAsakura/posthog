import re
import shlex
from uuid import UUID

from django.conf import settings

from posthog.utils import get_instance_region

from products.wizard.backend.facade.validation import is_executable_wizard_version
from products.wizard.backend.logic.runs.config import WIZARD_TIMEOUT_SECONDS

_WIZARD_PROGRAM_COMMAND_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def build_wizard_command(
    repository_path: str, team_id: int, wizard_version: str, program_command: tuple[str, ...]
) -> str:
    if not is_executable_wizard_version(wizard_version):
        raise ValueError("Invalid Wizard version")

    if any(_WIZARD_PROGRAM_COMMAND_PATTERN.fullmatch(argument) is None for argument in program_command):
        raise ValueError("Invalid Wizard program command")

    # review: for dev, we should set POSTHOG_API_URL to localhost
    parts = [
        f"cd {shlex.quote(repository_path)} &&",
        f"timeout -k 30 {WIZARD_TIMEOUT_SECONDS}",
        f"npx --yes {shlex.quote(f'@posthog/wizard@{wizard_version}')}",
        *(shlex.quote(argument) for argument in program_command),
        "--headless-DONOTUSE-EXPERIMENTAL",
        "--install-dir .",
        f"--region {shlex.quote(_wizard_region())}",
        f"--project-id {shlex.quote(str(team_id))}",
    ]

    if settings.DEBUG:
        parts.append('--base-url "$POSTHOG_API_URL"')

    return " ".join(parts)


def build_git_diff_command(repository_path: str) -> str:
    return f"cd {shlex.quote(repository_path)} && git add -N --all && git diff --binary --no-ext-diff HEAD"


def pull_request_branch(run_id: UUID) -> str:
    return f"posthog/wizard-{run_id.hex[:12]}"


def _wizard_region() -> str:
    return "eu" if get_instance_region() == "EU" else "us"
