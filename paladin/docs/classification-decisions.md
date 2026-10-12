# PALADIN document classification decisions

This record captures the Stage 0 decisions that govern how documents in
`paladin/data/documents/` are labelled in `paladin/data/manifest.json`. It is the
authoritative reference for the labels; the manifest carries the machine-readable
values, this file carries the human rationale.

## Clearance-level rubric (global)

Classification levels mean the same thing in every department:

- **Level 1 — general/internal.** Routine department guidance with no company-wide
  competitive or personal-harm risk if it spreads.
- **Level 2 — business-sensitive.** Disclosure could cause operational, financial,
  or personal harm. Budgets, compensation, and personnel data live here.
- **Level 3 — critical/executive.** Disclosure could cause serious strategic or
  reputational harm: board decisions, M&A, executive compensation, and
  security-critical infrastructure.

## Current labels and rationale (owner-confirmed)

All four current manifest labels were confirmed unchanged under the rubric above.
They are recorded as owner-approved decisions, not merely as whatever was in the
file.

| Document | Level | Department | Visibility | Rationale |
| --- | --- | --- | --- | --- |
| `engineering_policy.txt` | 1 | Engineering | department | Routine internal guidance on engineering access rules; no company-wide or personal-harm exposure. Level 1, scoped to the owning department. |
| `finance_guidelines.txt` | 2 | Finance | department | Budgets, salary, and personnel data — Level 2 under the rubric (financial/personal harm on disclosure). Scoped to Finance. |
| `executive_briefing.md` | 3 | Executive | department | Board decisions, M&A, executive compensation, security-critical infrastructure — Level 3 (serious strategic/reputational harm). Scoped to Executive. |
| `hr_handbook.md` | 2 | HR | company-wide | Conduct, data-handling, and personnel guidance — Level 2 content, but intended for all employees, so visibility is `company-wide` (department check waived by policy). |

Document wording alone does **not** determine classification or intended audience.
A document that *says* "all employees" is not automatically `company-wide`, and a
document that *says* "confidential" is not automatically Level 3. Classification
follows this rubric plus the owner's decision on intended readership.

## Ownership, approval, and ingestion (solo-operator model)

The project owner — **Gowtham K** — is the single **content owner**, **approver**,
and **ingestion operator** for every document. This is a deliberate scope
simplification for a solo-operator portfolio project. There is intentionally **no**
two-person rule, no CI enforcement gate, no CODEOWNERS requirement, and no
multi-party approval for classification changes. Those controls are documented as
future work should the project grow beyond a single operator.

Because one person both proposes and approves, the only durable evidence of
approval is the commit record (below) plus the content-hash binding in the
manifest.

## Recording approval: the commit trailer

Every commit that changes `paladin/data/manifest.json` must carry an approval
trailer of the exact form:

```
Approved-by: Gowtham K <YYYY-MM-DD> — Direction: tightened|broadened|unchanged
```

- `YYYY-MM-DD` is the actual date of approval.
- `Direction` describes the net effect of the manifest change on access:
  - `tightened` — access narrowed (higher level, narrower department, or
    visibility moved from `company-wide` to `department`).
  - `broadened` — access widened (lower level, broader department, or visibility
    moved to `company-wide`).
  - `unchanged` — labels confirmed with no net access change (including the first
    approved manifest, where all four labels were confirmed under the rubric).

The first approved manifest uses `Direction: unchanged`, with a commit-message
rationale stating that all four labels were confirmed against the rubric.

## Rationale required for future broadening changes

Before committing any change that **broadens** access to a document (lowers its
level, broadens its department, or moves it to `company-wide`), the owner writes a
one-paragraph rationale and includes it in the commit message. The paragraph must
explain why widening the audience is acceptable for that document's content.

## Content-hash binding (`content_sha256`)

Each manifest entry carries `content_sha256`, the SHA-256 of the document's exact
bytes. Ingestion (`data/ingest.py`) requires a well-formed digest for every entry
and recomputes it from the file before ingesting, failing closed on any mismatch.

The digest binds an approval to the specific bytes that were approved: if the
document changes after approval, the stored digest no longer matches and ingestion
stops until the owner re-reviews and updates the manifest (with a new
`Approved-by` trailer). **The hash does not authenticate who approved the change**
— it only proves the approved bytes are the bytes being ingested. Identity and
intent come solely from the commit trailer and the owner's own process.

## Future work

- Multi-operator approval enforcement (two-person rule for broadening changes,
  CODEOWNERS/CI gate) if the project grows past a solo owner.
- Independent verification of approval identity (the trailer is self-asserted).
