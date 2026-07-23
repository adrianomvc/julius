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

3. Determine the account scope from the user's request:

   - **Current/default account:** use the active AWS CLI credential chain without
     `--profile`.
   - **One named account:** use the explicitly named AWS CLI profile.
   - **All configured accounts:** only when the user explicitly says all, run
     `aws configure list-profiles`, then process each profile separately.
   - **Assume-role account:** use only an explicitly supplied role ARN.

   Never infer that "all accounts" is authorized from the mere presence of
   multiple profiles. Do not call AWS Organizations to expand the scope unless
   the user explicitly requests and authorizes it.

4. Before collecting each account, run one of:

   ```bash
   aws sts get-caller-identity
   aws sts get-caller-identity --profile <profile>
   ```

   Record account and ARN, without credentials. Confirm the intended account
   and that the configured identity/role is documented as read-only. Stop that
   account on mismatch; do not silently continue under another identity.

5. Use an exported dataset when supplied. For live collection, write one dataset
   per verified account:

   ```bash
   julius collect --output data/collected/current.json
   julius collect --profile <profile> --output data/collected/<profile>.json
   julius collect --profile <source-profile> --role-arn <role-arn> \
     --output data/collected/<account>.json
   ```

   Never reuse one account's output path for another account.

6. Generate a separate agent workspace for each account:

   ```bash
   julius agent prepare --input <dataset.json> --output data/agent/<account>
   ```

7. Read that account's `instructions.md`, `context.json` and
   `output-schema.json`. Analyze only opportunities present in that context.
8. Prefer evidence in the context. When evidence is absent, explicitly list it
   under `missing_evidence`.
9. For every implementation recommendation, provide at least one relevant link
   under `https://docs.aws.amazon.com/`. Never invent a URL. Open and verify the
   page before returning it.
10. Write only the structured result to that account's `result.json`.
11. Validate it locally:

    ```bash
    julius agent validate \
      --context data/agent/<account>/context.json \
      --result data/agent/<account>/result.json
    ```

12. For multiple accounts, validate every account independently before creating
    a portfolio summary. Never merge evidence or opportunity IDs across accounts.
13. Generate the enriched artifacts for each account:

    ```bash
    julius report \
      --input data/collected/<account>.json \
      --output data/reports/<account> \
      --agent-context data/agent/<account>/context.json \
      --agent-result data/agent/<account>/validated-result.json

    julius notify \
      --mode dry-run \
      --input data/collected/<account>.json \
      --outbox data/outbox \
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
