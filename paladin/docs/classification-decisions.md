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

### What `company-wide` does and does not waive

`visibility: "company-wide"` removes the **department** restriction only. The
**clearance gate still applies**: a user whose clearance is below the
document's level is denied even when visibility is `company-wide`. Concretely,
`emp1` (clearance 1) is denied the Level 2 HR handbook
(`INSUFFICIENT_CLEARANCE`), while `mgr1` (clearance 2, same department as
`emp1`) is allowed. This is asserted by
`test_company_wide_still_requires_clearance` in
`tests/test_abac_policy.py`, and the four labels above are unchanged by it.

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

`content_sha256` is a SHA-256 **cryptographic hash of document bytes only**.
Ingestion (`data/ingest.py`) requires a well-formed lowercase-hex digest for
every entry, recomputes it from the exact bytes on disk, and fails closed on
any mismatch. Verified bytes are passed as snapshots to the chunking stage, so
the ingested content cannot differ from the verified bytes even if the file
changes mid-run; `_verified_bytes` state is cleared and fully replaced at the
start of every ingestion attempt.

Scope limits (read carefully):

- The digest does **not** validate classification fields. `level`,
  `department`, and `visibility` are enforced only by owner review
  (checklist below) and by the ABAC policy at query time.
- The digest is **not an authenticated signature**. It binds an approval to
  the specific bytes that were approved — if the document changes after
  approval, the stored digest no longer matches and ingestion stops until the
  owner re-reviews and updates the manifest (with a new `Approved-by`
  trailer) — but it cannot prove *who* approved.
- The commit trailer is a **manual, non-cryptographically-enforced approval
  record**: a self-asserted text line in the git history, verified by human
  review, not by code. Identity and intent come solely from the commit
  trailer and the owner's own process. Independent verification of approval
  identity is future work (below).

No second hash is added to the manifest for approval authentication: a hash
over the classification fields would attest to *what* was approved but still
could not attest to *who* approved it, so it would add complexity without
closing the identity gap. Metadata-integrity protection beyond the digest is
therefore deferred to the multi-operator stage, where approver signatures
could carry a clear, tested purpose.

## Future work

- Multi-operator approval enforcement (two-person rule for broadening changes,
  CODEOWNERS/CI gate) if the project grows past a solo owner.
- Independent verification of approval identity (the trailer is self-asserted).

## Versioned-generation promotion (current implementation)

`data/ingest.py` no longer promotes by mutating a live collection. Every
ingestion builds a **new, immutable generation collection**
(`paladin_docs_abac_gen_NNNNNN`), validates it in full, and only then flips the
authoritative pointer — the single `is_active` row in the PostgreSQL
`vector_generations` table — inside one transaction. Cutover is therefore a
pointer flip, never a copy and never an in-place rewrite.

Guarantees this gives, each covered by tests in
`tests/test_generation_cutover.py` and `tests/test_generation_pointer.py`:

- **A half-built generation is never served.** Until the flip, the new
  collection is invisible; queries resolve only the active generation. A
  failure at any point before the flip (chunking, validation, empty result,
  unregistered activation) leaves the previous generation serving untouched.
- **No mixed old/new rows.** A request resolves the generation once, before it
  queries, so it cannot observe a partially replaced index.
- **A failed run leaves no partial state.** The un-activated generation is
  discarded best-effort (collection then row); a later run's retention pass
  sweeps anything left behind.
- **Rollback is safe and verified.** `--rollback` flips to the most recently
  deactivated generation, and only after probing that its collection still
  exists in Chroma; a missing target is refused with the pointer untouched and
  the row retained for a restored collection.
- **Retention protects rollback and in-flight requests.** Cleanup keeps the
  active generation, the rollback target, the legacy bootstrap collection, and
  any generation whose `deactivated_at` (the timestamp stamped at cutover) is
  less than `INGEST_IN_USE_GRACE_SECONDS` (900s) ago — the gateway resolves the
  pointer per request, so a request that opened a generation just before a
  cutover can still be reading it. The window runs from deactivation, not
  activation, so a generation that served for weeks keeps its full grace
  period after losing the pointer. Cleanup is best-effort, never fatal, and
  retried on every run.
- **The one-active invariant is enforced by the database.** A partial unique
  index keyed on `is_active` (not on `name`, which only forbade a duplicated
  active name) makes two active rows impossible; `acquire()` replaces an index
  of the older shape and migrates columns introduced after the first
  deployment (`deactivated_at`) in place, because `create_all` never alters an
  existing table. If a pre-existing table already holds multiple active rows
  the new index cannot be created — deliberate fail-closed behaviour: the
  operator must resolve the corrupt state first.
- **Ingestion is serialised.** A PostgreSQL session-level advisory lock
  refuses concurrent runs; the lock and the connection are released on every
  failure path, including a failed bootstrap.
- **Pointer failures fail closed.** A pointer table with rows but no active row
  is treated as corrupt (503), not as "no pointer yet"; only a genuinely
  missing table (driver `SQLSTATE 42P01`, or SQLite's `no such table`) resolves
  to the legacy fallback.

Residual limitation (unchanged in kind from the previous design): promotion
still depends on the pointer store. If PostgreSQL is unreachable the gateway
fails closed as unavailable rather than serving a stale collection — an
availability trade-off, not an access one. The ABAC policy filters whatever
rows are present, so no failure mode here can widen access.
