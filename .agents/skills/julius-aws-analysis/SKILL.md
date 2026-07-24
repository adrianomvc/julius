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

Julius code owns:

- collection and normalized inventory;
- opportunity IDs and fingerprints;
- estimated gain and financial calculations;
- difficulty, confidence, priority and governance classification;
- lifecycle and realized-benefit validation.

You own only contextual enrichment:

- inspect available scripts, SQL and ASL definitions;
- explain the likely technical cause using cited evidence;
- improve the clarity of the recommended action;
- identify dependencies and conflicts between recommendations;
- propose a safe implementation order;
- provide implementation and validation steps;
- find relevant official AWS documentation.

Never overwrite a deterministic field. If new evidence contradicts one, record
the contradiction in `missing_evidence` or `assumptions`; do not recalculate it.

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
   `output-schema.json`. Analyze only opportunities present in that context.
   Read only technical files referenced by `technical_artifacts`.
10. Prefer evidence in the context. When evidence is absent, explicitly list it
   under `missing_evidence`.
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
- Every documentation URL is HTTPS on `docs.aws.amazon.com`.
- No AWS resource was changed and no e-mail was sent.
