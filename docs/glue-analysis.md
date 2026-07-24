# AWS Glue analysis in Julius

The Glue portfolio is deterministic and read-only. It inventories Glue ETL
jobs regardless of authoring mode (`SCRIPT`, `VISUAL`, or `NOTEBOOK`) and keeps
Interactive Sessions, Crawlers, Glue Triggers, and DataBrew as distinct asset
types.

## Cost contract

- USD is the canonical currency for billing and modeled values. No implicit
  foreign-exchange conversion is performed.
- Cost Explorer `UnblendedCost` is the AWS billing source at service level.
  During an open month it is month-to-date, can be estimated, and can lag.
- The current billing month starts on day 1 and ends at `data_through`.
- `DPUSeconds / 3600` is recorded as AWS-reported DPU-hours only when that
  field is available.
- Fixed capacity without `DPUSeconds` is recorded separately as estimated
  DPU-hours using billable duration and effective capacity.
- DataBrew uses estimated node-hours and its own versioned price.
- Month-to-date cost and end-of-month forecast are never presented as the same
  value.
- Process currency values are modeled as consumption multiplied by a
  versioned USD rate. They are not labeled as invoice values.
- Shared jobs are split across process roots; their cost is not duplicated.
- Bottom-up process costs are reconciled with Cost Explorer only when both use
  the same currency.
- Savings across related assets consume a shared process cap so the portfolio
  cannot reserve the same process spend twice.

## Collection health contract

Every live collection records sanitized health telemetry per source: status,
start/end, duration, collected and expected items, coverage, data freshness,
stable error category, impact and next action. Raw exception messages are never
persisted.

STS identity and Glue Jobs are required sources. Their failure blocks the
collection. Optional sources degrade the scan to `partial`; deliberately
disabled governance enrichments are visible but do not degrade Glue monitoring.
CloudWatch and Spark-log coverage is measured against the collected Glue jobs,
and missing evidence keeps dependent recommendations blocked.

Glue scripts are collected in a separate read-only stage. Supplying the
artifact manifest replaces the `separate_stage_required` entry with verified
script coverage. Hash mismatch or a path outside the artifact bundle aborts the
analysis.

Every financial opportunity is reduced by the configured conservative
realization factor and capped by both its measured baseline and the attributed
process forecast. Findings that lack memory, disk, spill, incremental-volume,
or activity evidence remain investigations with no quantified saving.

## Glue code cost analysis

When a verified read-only artifact manifest is supplied, Julius validates each
script hash and runs deterministic static rules against Glue `ScriptLocation`
content. Reports expose only the rule, artifact hash and line numbers, never
source snippets or secrets.

The active rules cover:

- catalog reads without an explicit predicate pushdown and direct S3 reads
  filtered only after loading;
- JDBC reads without observable partitioning;
- forced single partitions, excessive output partitions and writes in loops;
- driver materialization with `collect`, `toPandas` or `toLocalIterator`;
- Python UDFs, repeated Spark actions and iterative query-plan construction;
- cache/persist without an observable unpersist;
- external I/O inside functions passed to UDF/map/foreach;
- full overwrite without an observable incremental scope;
- shuffle-producing joins, aggregations and repartitioning;
- extreme fixed shuffle partition counts;
- swallowed exceptions that can lead to manual retries;
- bookmark transformation context and missing commit review; and
- Spark ETL scripts with no distributed API usage that may fit Python Shell.

Static evidence alone is not treated as realized saving. Code findings remain
blocked investigations until runtime metrics and an A/B benchmark confirm the
same input, equivalent output, duration and DPUSeconds. Blocked findings do not
reserve the shared financial cap of confirmed actions.

The Spark-to-Python-Shell rule additionally requires a complete script, a
`glueetl` job, no bookmarks and no incompatible extra-file/JAR arguments. Its
initial model compares observed Spark cost with a 0.0625-DPU Python Shell
scenario, but a separate pilot must validate Python 3.9 libraries, local memory,
temporary storage, runtime and output before any job definition is changed.

## Ownership contract

Ownership precedence is:

1. explicit `Owner` tag;
2. latest human update event in CloudTrail;
3. human creator event;
4. existing corporate/data-product fallbacks;
5. unknown.

Run events identify an operator, not an owner. AWS service identities and
recognizable infrastructure/CI roles are not promoted to people. CloudTrail
Event History covers only the latest 90 days, so missing creation evidence is
reported instead of inferred.

## Process boundary

The current graph supports:

```text
EventBridge schedule -> Step Functions -> Glue Job
Glue Trigger -> Glue Job or Crawler
Glue Job -> catalog table -> consumer
```

Jobs without a known orchestrator become their own process root. Interactive
Sessions and DataBrew jobs remain separate roots unless an explicit
relationship is collected.

## Evidence used

- Glue `GetJobs` and `GetJobRuns`
- Glue `ListSessions`
- Glue `GetCrawlers`, `GetCrawlerMetrics`, and `ListCrawls`
- Glue `GetTriggers`
- DataBrew `ListJobs`, `ListJobRuns`, and `ListSchedules`
- CloudWatch Glue Observability metrics
- Spark event logs from the job-configured S3 prefix, limited to 20 recent
  objects of at most 10 MiB each; incomplete evidence never becomes a
  synthetic zero
- CloudTrail management events
- Cost Explorer for service-level reconciliation

Relevant AWS references:

- <https://docs.aws.amazon.com/glue/latest/webapi/API_JobRun.html>
- <https://docs.aws.amazon.com/glue/latest/dg/monitor-observability.html>
- <https://docs.aws.amazon.com/glue/latest/dg/monitor-spark-ui-jobs.html>
- <https://docs.aws.amazon.com/glue/latest/dg/auto-scaling.html>
- <https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-pushdown.html>
- <https://docs.aws.amazon.com/glue/latest/dg/add-job-python.html>
- <https://docs.aws.amazon.com/glue/latest/dg/pyshell-migration.html>
- <https://docs.aws.amazon.com/glue/latest/webapi/API_Job.html>
- <https://docs.aws.amazon.com/prescriptive-guidance/latest/tuning-aws-glue-for-apache-spark/optimize-shuffles.html>
- <https://docs.aws.amazon.com/glue/latest/dg/aws-glue-api-crawler-crawling.html>
- <https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html>
- <https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostForecast.html>
- <https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-price-list-query-api.html>
- <https://docs.aws.amazon.com/cur/latest/userguide/what-is-data-exports.html>
- <https://docs.aws.amazon.com/databrew/latest/dg/jobs.html>
- <https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events-cli.html>
