from datetime import timedelta

from products.tasks.backend.facade.sandbox import SandboxTemplate

WIZARD_TIMEOUT_SECONDS = 45 * 60
WIZARD_TIMEOUT_EXIT_CODE = 124
WIZARD_ERROR_DETAIL_LENGTH = 2000

SANDBOX_EXECUTION_TIMEOUT_SECONDS = WIZARD_TIMEOUT_SECONDS + 120
SANDBOX_TTL_SECONDS = 75 * 60
SANDBOX_TEMPLATE_BASE = SandboxTemplate.DEFAULT_BASE
SANDBOX_MEMORY_GB = 4
SANDBOX_CPU_CORES = 2
SANDBOX_DISK_SIZE_GB = 16

RECONCILIATION_BATCH_SIZE = 100

CLOUD_RUN_HOURLY_LIMIT = 2
CLOUD_RUN_HOURLY_WINDOW = timedelta(hours=1)
CLOUD_RUN_DAILY_LIMIT = 5
CLOUD_RUN_DAILY_WINDOW = timedelta(days=1)

# review: pr title should be better, and body should come from the wizard handoff
PULL_REQUEST_TITLE = "Set up PostHog"
PULL_REQUEST_BODY = "This pull request contains changes created by Wizard, PostHog's setup agent."
PULL_REQUEST_COMMIT_MESSAGE = "Set up PostHog"
