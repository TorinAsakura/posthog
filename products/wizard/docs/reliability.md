# Wizard Cloud Run Reliability

These are internal targets. They are not a customer SLA.

Measure each SLI over one calendar month.

| SLI                         | Definition                                                                      | Initial SLO  |
| --------------------------- | ------------------------------------------------------------------------------- | ------------ |
| Dispatch success            | Accepted cloud runs that start a Temporal workflow.                             | 99.0%        |
| Worker provisioning success | Dispatched runs that provision a Wizard Worker.                                 | 99.0%        |
| Platform failure rate       | Runs that fail because of PostHog, Temporal, a sandbox, or artifact publishing. | At most 2.0% |
| Artifact handoff success    | Successful Wizard executions that create the expected artifact.                 | 99.0%        |
| Worker cleanup success      | Worker cleanup attempts that destroy the Wizard Worker.                         | 99.5%        |
| Deadline breach rate        | Cloud runs detected after their deadline.                                       | At most 0.1% |

Do not count user input failures as platform failures. Examples include an inaccessible repository and an invalid repository configuration.

Collect latency data before you set latency SLOs. Review these targets after 30 to 60 days of production data.
