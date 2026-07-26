---
name: julius-aws-analysis
description: Analyze AWS Consumer accounts with the Julius CLI, enrich deterministic opportunities with contextual reasoning, and return safe implementation guidance with official AWS documentation.
---

# Julius AWS Analysis

Use this skill when asked to analyze AWS costs or governance with Julius.

## Non-negotiable safety boundary

- Treat AWS as read-only.
- Never run create, update, put, delete, stop, terminate, modify, tag, untag,
  deploy, apply, import, restore, or mutation commands.
- Never send e-mail during analysis.
- Never print, persist, or return credentials, tokens, secrets, data rows, or
  personal information.
- Do not bypass an approval request.
- Stop and report the exact blocker if read-only identity cannot be verified.
- Recommendations are proposals only; a human must approve implementation.

## Deterministic versus AI responsibilities

The split is by certainty, not by service. Julius keeps what it can prove: the
trigger is a declared AWS property or a measured metric, the conclusion is
single, and the saving follows from the fact. You get what has N variables —
reading a script, a SQL statement or a dependency chain to decide whether
something is waste *here*. `collect()` over a hundred rows is correct and over
a hundred million it is waste; the same AST produces both, so no threshold can
settle it and you can.

Julius code owns:

- collection and normalized inventory;
- opportunity IDs and fingerprints;
- estimated gain and financial calculations;
- difficulty, confidence, priority and governance classification;
- lifecycle and realized-benefit validation.

You own four tasks, in this order:

1. **Judge every signal.** `context.json` carries a `signals` array: static code
   patterns and configuration observations that Julius detected but cannot
   conclude on its own. Each one gives you the observation, the question to
   answer, the artifact hash, the line numbers and the evidence still missing.
   Read the complete artifact and return `confirmed`, `rejected` or
   `needs_evidence` with a rationale. Every signal must come back judged —
   silence is not a verdict, and the validator rejects an incomplete set.
2. **Enrich the deterministic opportunities.** Explain the likely technical
   cause from cited evidence, sharpen the recommended action, identify
   dependencies and conflicts, propose a safe implementation order, and give
   implementation and validation steps with official AWS documentation.
3. **Decide the side of a trade-off.** Where a recommendation admits two paths —
   adjusting the job that writes or the query that reads — pick one and say who
   breaks with the choice. `XSVC-WASTED-PRODUCTION` is the standing case.
4. **Report what the catalog misses.** Waste you observed that no `rule_id` in
   the package covers goes to `uncovered_findings`, with a `proposed_rule_id`
   that does not collide with an existing rule. These carry no financial value
   and no ranking position: they are proposals for a new deterministic rule,
   accumulated across scans and accounts for human review.

Never overwrite a deterministic field. If new evidence contradicts one, record
the contradiction in `missing_evidence` or `assumptions`; do not recalculate it.

Never assign a saving to a signal or to an uncovered finding. If a rule fired
without the metric that would quantify it, the missing metric is the answer —
say what is absent under `missing_evidence` instead of estimating it.

Read `constraints.rule_families_without_evidence` before concluding anything
about coverage. Those rule families produced nothing because their inventory
arrived empty; absence of findings there is not absence of problems. The same
holds for any source marked partial or unavailable in
`constraints.collection_health` — report it as missing evidence, never as zero.

## Procedure

1. Read `AGENTS.md` and `README.md`.
2. Confirm the repository is Julius and install it with:

   ```bash
   python -m pip install -e ".[aws]"
   ```

3. Use only AWS CLI SSO identities explicitly configured for Julius:

   - the Julius region is always `sa-east-1`;
   - never use `--role-arn`;
   - use `--sso-profile` only with a profile explicitly enabled in the local
     Julius account registry;
   - never read, copy, print or persist access keys, secrets, session tokens or
     files from the AWS SSO cache.

   Do not list profiles, discover accounts through AWS Organizations or expand
   the scope implicitly.

4. Require `~/.julius-accounts.json` based on
   `.julius-accounts.example.json`. It contains only the logical account name,
   expected Account ID, non-secret SSO profile reference and the explicit
   `enabled` flag. Verify every enabled identity before collection:

   ```bash
   julius agent verify-accounts \
     --config ~/.julius-accounts.json \
     --output data/agent/verified-accounts.json
   ```

5. Before collecting a configured account, run:

   ```bash
   aws sts get-caller-identity --profile <sso-profile>
   ```

   Record account and ARN, without credentials. Confirm the Account ID matches
   the enabled entry and that the SSO permission set is documented as
   read-only. Stop on mismatch; do not silently continue under another
   identity.

6. Use an exported dataset when supplied. For live collection, write one dataset
   per verified account:

   ```bash
   julius collect --sso-profile <sso-profile> \
     --output data/collected/<account>.json
   ```

   The command always uses `sa-east-1` and the active AWS credential chain.
   Never reuse one account's output path for another account.

7. Collect the bounded technical artifacts using the same verified identity:

   ```bash
   julius agent collect-artifacts \
     --input data/collected/<account>.json \
     --sso-profile <sso-profile> \
     --output data/artifacts/<account>
   ```

   This command uses the same active SSO identity and may call only STS, S3
   GetObject and Step Functions list/describe operations. Stop on identity
   mismatch.

8. Generate a separate agent workspace for each account:

   ```bash
   julius agent prepare \
     --input <dataset.json> \
     --output data/agent/<account> \
     --artifacts-manifest data/artifacts/<account>/manifest.json
   ```

9. Read that account's `instructions.md`, `context.json` and
   `output-schema.json`. Analyze only opportunities present in that context and
   judge only signals present in that context. Read only technical files
   referenced by `technical_artifacts`. Check `portfolio` to see how much of the
   account the package covers — it is a ranked slice, not the whole portfolio.
10. Prefer evidence in the context. When evidence is absent, explicitly list it
   under `missing_evidence`. Any conclusion about a script must cite the
   `sha256` of that script under `evidence_ref`; a configuration signal has no
   artifact, so its `evidence_ref.sha256` is the empty string.
11. For every implementation recommendation, provide at least one relevant link
   under `https://docs.aws.amazon.com/`. Never invent a URL. Open and verify the
   page before returning it.
12. Write only the structured result to that account's `result.json`.
13. Validate it locally:

    ```bash
    julius agent validate \
      --context data/agent/<account>/context.json \
      --result data/agent/<account>/result.json
    ```

14. Validate every enabled account independently before creating a portfolio
    summary. Never merge evidence or opportunity IDs across accounts.
15. Generate the enriched artifacts for each account:

    ```bash
    julius report \
      --input data/collected/<account>.json \
      --output data/reports/<account> \
      --artifacts-manifest data/artifacts/<account>/manifest.json \
      --agent-context data/agent/<account>/context.json \
      --agent-result data/agent/<account>/validated-result.json

    julius notify \
      --mode dry-run \
      --input data/collected/<account>.json \
      --outbox data/outbox \
      --artifacts-manifest data/artifacts/<account>/manifest.json \
      --agent-context data/agent/<account>/context.json \
      --agent-result data/agent/<account>/validated-result.json
    ```

    Active e-mail is a separate human-approved operation. Do not use
    `--mode active` as part of this analysis skill.

## Completion criteria

- `account` and `scan_id` exactly match the context.
- Every recommendation references an existing `opportunity_id`.
- No deterministic score or financial value is changed.
- Implementation order contains no unknown or duplicate ID.
- Facts, assumptions and missing evidence are distinguishable.
- Dependencies, conflicts, risks, implementation steps and validation steps
  are present, even when their arrays are empty.
- `contextual_diagnosis` and `recommendation` are non-empty, and each
  recommendation has at least one `implementation_step` or one
  `missing_evidence` entry — either you say how to act, or you say what is
  missing before anyone can.
- A recommendation with implementation steps carries at least one
  documentation reference.
- Every signal in the context has exactly one verdict, and no verdict names a
  signal outside the context.
- Every `evidence_ref` for a code claim matches the `sha256` of that signal's
  own artifact.
- No `proposed_rule_id` collides with a rule already in the package.
- Every documentation URL is HTTPS on `docs.aws.amazon.com`.
- No AWS resource was changed and no e-mail was sent.
