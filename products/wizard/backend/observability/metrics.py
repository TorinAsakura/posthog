from prometheus_client import Counter, Histogram

from products.wizard.backend.facade.contracts import WizardRunDTO
from products.wizard.backend.facade.enums import WizardRunStage, WizardRunStatus
from products.wizard.backend.observability.config import (
    WIZARD_RUN_DURATION_BUCKETS,
    WIZARD_WORKER_CPU_SECONDS_BUCKETS,
    WIZARD_WORKER_DISK_GB_SECONDS_BUCKETS,
    WIZARD_WORKER_LIFETIME_BUCKETS,
    WIZARD_WORKER_MEMORY_GB_SECONDS_BUCKETS,
)
from products.wizard.backend.observability.contracts import WizardRunDispatchOutcome, WizardWorkerUsageObservation

WIZARD_RUNS_CREATED_TOTAL = Counter(
    "posthog_wizard_runs_created_total",
    "Wizard runs created",
    labelnames=["environment"],
)

WIZARD_RUN_DISPATCH_ATTEMPTS_TOTAL = Counter(
    "posthog_wizard_run_dispatch_attempts_total",
    "Wizard cloud run dispatch attempts",
    labelnames=["outcome"],
)

WIZARD_RUN_STAGE_ENTERED_TOTAL = Counter(
    "posthog_wizard_run_stage_entered_total",
    "Wizard cloud runs entering each execution stage",
    labelnames=["stage"],
)

WIZARD_RUNS_FINISHED_TOTAL = Counter(
    "posthog_wizard_runs_finished_total",
    "Wizard runs that reached a terminal status",
    labelnames=["environment", "status", "error_code"],
)

WIZARD_RUN_FAILURES_TOTAL = Counter(
    "posthog_wizard_run_failures_total",
    "Failed Wizard runs by execution stage and error code",
    labelnames=["environment", "stage", "error_code"],
)

WIZARD_RUN_DURATION_SECONDS = Histogram(
    "posthog_wizard_run_duration_seconds",
    "Wizard run duration",
    labelnames=["environment", "status"],
    buckets=WIZARD_RUN_DURATION_BUCKETS,
)

WIZARD_GIT_DIFFS_OMITTED_TOTAL = Counter(
    "posthog_wizard_git_diffs_omitted_total",
    "Wizard git diff artifacts omitted because they exceeded the size limit",
    labelnames=["environment"],
)

WIZARD_WORKER_LIFETIME_SECONDS = Histogram(
    "posthog_wizard_worker_lifetime_seconds",
    "Wizard Worker sandbox lifetime",
    buckets=WIZARD_WORKER_LIFETIME_BUCKETS,
)

WIZARD_WORKER_CPU_USAGE_SECONDS = Histogram(
    "posthog_wizard_worker_cpu_usage_seconds",
    "Wizard Worker measured CPU usage",
    buckets=WIZARD_WORKER_CPU_SECONDS_BUCKETS,
)

WIZARD_WORKER_BILLED_CPU_USAGE_SECONDS = Histogram(
    "posthog_wizard_worker_billed_cpu_usage_seconds",
    "Wizard Worker provider-billed CPU usage",
    buckets=WIZARD_WORKER_CPU_SECONDS_BUCKETS,
)

WIZARD_WORKER_ALLOCATED_CPU_CORE_SECONDS = Histogram(
    "posthog_wizard_worker_allocated_cpu_core_seconds",
    "Wizard Worker allocated CPU core-seconds",
    buckets=WIZARD_WORKER_CPU_SECONDS_BUCKETS,
)

WIZARD_WORKER_ALLOCATED_MEMORY_GB_SECONDS = Histogram(
    "posthog_wizard_worker_allocated_memory_gb_seconds",
    "Wizard Worker allocated memory GB-seconds",
    buckets=WIZARD_WORKER_MEMORY_GB_SECONDS_BUCKETS,
)

WIZARD_WORKER_ALLOCATED_DISK_GB_SECONDS = Histogram(
    "posthog_wizard_worker_allocated_disk_gb_seconds",
    "Wizard Worker allocated disk GB-seconds",
    buckets=WIZARD_WORKER_DISK_GB_SECONDS_BUCKETS,
)


def report_run_created(run: WizardRunDTO) -> None:
    WIZARD_RUNS_CREATED_TOTAL.labels(environment=run.environment.value).inc()


def report_dispatch_finished(outcome: WizardRunDispatchOutcome) -> None:
    WIZARD_RUN_DISPATCH_ATTEMPTS_TOTAL.labels(outcome=outcome.value).inc()


def report_stage_entered(stage: WizardRunStage) -> None:
    WIZARD_RUN_STAGE_ENTERED_TOTAL.labels(stage=stage.value).inc()


def report_run_finished(run: WizardRunDTO, failure_stage: WizardRunStage | None) -> None:
    error_code = run.error_code.value if run.error_code is not None else "none"

    WIZARD_RUNS_FINISHED_TOTAL.labels(
        environment=run.environment.value,
        status=run.status.value,
        error_code=error_code,
    ).inc()

    if run.status == WizardRunStatus.FAILED:
        WIZARD_RUN_FAILURES_TOTAL.labels(
            environment=run.environment.value,
            stage=failure_stage.value if failure_stage is not None else "none",
            error_code=error_code,
        ).inc()

    if run.started_at is None or run.finished_at is None:
        return

    duration = (run.finished_at - run.started_at).total_seconds()
    if duration >= 0:
        WIZARD_RUN_DURATION_SECONDS.labels(environment=run.environment.value, status=run.status.value).observe(duration)


def report_worker_usage(usage: WizardWorkerUsageObservation) -> None:
    if usage.lifetime_seconds < 0:
        return

    WIZARD_WORKER_LIFETIME_SECONDS.observe(usage.lifetime_seconds)
    WIZARD_WORKER_ALLOCATED_CPU_CORE_SECONDS.observe(usage.allocated_cpu_core_seconds)
    WIZARD_WORKER_ALLOCATED_MEMORY_GB_SECONDS.observe(usage.allocated_memory_gb_seconds)
    WIZARD_WORKER_ALLOCATED_DISK_GB_SECONDS.observe(usage.allocated_disk_gb_seconds)

    if usage.cpu_usage_seconds is not None:
        WIZARD_WORKER_CPU_USAGE_SECONDS.observe(usage.cpu_usage_seconds)

    if usage.billed_cpu_usage_seconds is not None:
        WIZARD_WORKER_BILLED_CPU_USAGE_SECONDS.observe(usage.billed_cpu_usage_seconds)


def report_git_diff_omitted(run: WizardRunDTO) -> None:
    WIZARD_GIT_DIFFS_OMITTED_TOTAL.labels(environment=run.environment.value).inc()
