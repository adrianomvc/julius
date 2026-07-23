# Julius agent instructions

Julius is an AI-assisted AWS optimization portfolio. The deterministic Python
engine is the source of truth for evidence, savings, difficulty, confidence,
priority, classification and lifecycle. Devin may enrich context but must not
replace or silently alter those values.

All AWS inspection is read-only. Never create, update, stop, delete, tag,
deploy, apply or otherwise mutate AWS resources. Never send an active e-mail
during analysis. Real AWS and e-mail validation require explicit human
approval on the work machine.

Use the AWS CLI credential chain already configured in the execution machine.
The scope may be the current account, one named profile, or all configured
profiles only when the user explicitly requests all. Verify every identity with
STS before collection and keep outputs isolated per account.

Use `.agents/skills/julius-aws-analysis/SKILL.md` for account analysis. Return
structured, evidence-linked recommendations and use only official AWS
documentation links under `https://docs.aws.amazon.com/`.
