# AWS Glue analysis in Julius

The Glue portfolio is deterministic and read-only. It inventories Glue ETL
jobs regardless of authoring mode (`SCRIPT`, `VISUAL`, or `NOTEBOOK`) and keeps
Interactive Sessions, Crawlers, Glue Triggers, and DataBrew as distinct asset
types.

## What the deterministic layer is for

The split between Python and the contextual analysis is not by service. It is
by how much the evidence closes.

**Python owns what it can prove.** A rule belongs here when three things hold:
the trigger is a fact — a declared AWS property or a measured metric, never a
syntactic pattern or a distance from some default; the conclusion is single —
two competent people looking at the same data reach the same action; and the
saving follows from the fact, without assuming intent or business need. A
timeout of 480 minutes on a job that runs for 12, a failure rate of 30% billing
DPU-hours to the point of failure, an endpoint running 24/7 with zero
invocations: those close. Python states them, prices them, and ranks them.

**The contextual analysis owns what has N variables.** Reading a script, a SQL
statement, or a dependency chain to decide whether the code is wasteful *here*
is not something a threshold can do. `collect()` over a hundred rows is
correct; over a hundred million it is waste, and the same AST produces both.
Whether migrating a runtime is safe depends on libraries only the script
reveals. Whether a job running 720 times a month is excessive depends on the
source it reads. These reach the AI as **signals** — the observation, the
artifact hash, the lines, and the evidence still missing — and come back
confirmed, rejected, or needing evidence.

Neither layer crosses. The AI never computes or alters a saving, a difficulty,
a confidence or a priority; Python never asserts waste from a pattern it cannot
corroborate. When a rule fires but the metric that would quantify it was not
collected, the honest outcome is a blocked investigation with no saving — the
fix is collection, not judgement.

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

## Runtime cost analysis

In addition to capacity, failure, timeout, version, FLEX, bookmark, schedule,
and reconciliation rules, the runtime analysis records:

- active and overlapping time for job runs in the analysis window;
- runs linked to a previous run as retry evidence;
- `MaxConcurrentRuns` and job-run queuing;
- bytes read, bytes written, files written, and streaming records when
  CloudWatch publishes the corresponding datapoints; and
- long-running streaming executions that started before the analysis window
  but remained active inside it.

The following runtime findings are conservative investigations:

- `GLUE-OVERLAPPING-RUNS` identifies concurrent runs of the same batch job,
  but does not assume that parameterized parallelism is duplicate work;
- `GLUE-STREAMING-NO-INPUT` identifies a streaming job that consumed capacity
  while an explicit CloudWatch datapoint reported zero records; and
- `GLUE-NO-INPUT-WASTE` identifies a batch job that consumed DPU-hours while
  an explicit throughput datapoint reported zero bytes read.

Missing CloudWatch datapoints remain `None` and never become synthetic zero.
These findings do not estimate savings until schedule, SLA, input, arguments,
and output equivalence are confirmed.

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

A static pattern is not a finding. The same `collect()` is correct over a
hundred rows and wasteful over a hundred million, and neither the AST nor a
threshold can tell the two apart. So the split is made by measured runtime
evidence:

- with a correlated runtime metric — memory, disk, spill, shuffle bytes, files
  and bytes written — the pattern becomes an `Opportunity`. It stays a blocked
  investigation until an A/B benchmark confirms the same input, equivalent
  output, duration and DPUSeconds, and it does not reserve the shared financial
  cap of confirmed actions;
- without that metric it becomes a `Signal`: the same observation, carrying the
  artifact hash, the line numbers and the evidence still missing, but with no
  estimated saving, no backlog entry and no position in the ranking. It is
  judged by the contextual analysis against the complete script, which either
  confirms it, rejects it, or names the evidence it needs.

Two patterns are exempt because the code closes the conclusion on its own: a job
with bookmarks enabled and no observable `job.commit()`, and the
Spark-to-Python-Shell candidate, whose gates already require a complete script,
a `glueetl` command, no bookmarks and no incompatible arguments.

The small-files code finding uses measured `filesWritten` and `bytesWritten`
when available. Static evidence without those metrics remains uncorrelated and
does not receive a modeled saving.

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

Data Catalog metadata is used only as read-only evidence for tables created in
the account's shared database, ownership, and lineage. Julius does not produce
Data Quality, global Data Catalog, partition-index, column-statistics, or table
optimizer recommendations.

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
