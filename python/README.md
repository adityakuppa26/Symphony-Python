# Symphony Jira Python MVP

It uses Jira REST credentials from `WORKFLOW.md`, prepares a per-issue local
workspace, runs the locally installed `codex` CLI with `codex exec --json`, stores
run data in SQLite, and posts Jira comments from the Python orchestrator.

It does not require `OPENAI_API_KEY` or `CODEX_API_KEY`. Codex authentication is
whatever the local `codex` CLI already uses.

## Development workflow

`WORKFLOW.md` is the single supported workflow. Symphony inspects Jira requirements
and the relevant application code, plans the smallest correct change using existing
patterns, waits for human plan approval, implements, runs an independent code review
and correction loop, and hands off. Changes to approved scope require a new plan
and approval.

The development handler owns changes in `foyr2`, `cpm`, and `pi`. Workspaces remain
under `~/codex-workspaces/development`; run history, approvals, and review context
remain in `.symphony/development.sqlite3`. Issues are selected with `codex-ready`.
Run the polling process and dashboard against the same workflow:

```sh
python3 -m symphony_jira run ./WORKFLOW.md
python3 -m symphony_jira dashboard ./WORKFLOW.md --port 3333
```

After handoff, submit **Address Human Review** on the completed run. Symphony
resumes the same workspace using the frozen Jira snapshot, PlanSpec, approval,
previous final response, review history, and current diff. Process restarts retain
this context in the database. In-scope corrections re-enter implementation and
review; scope changes return to planning and approval.

When review sends an implementation back to planning, Symphony preserves its
tracked, staged, and untracked changes. The revised planner receives the previous
approved plan, implementation report, review, and exact workspace diff. The
dashboard shows that retained diff with the revised plan. Approval binds both;
changes during planning, before approval, or before execution require replanning.
Initial planning still requires a clean worktree. Failed replan attempts retain
the earlier approved execution history so retrying does not require discarding code.

All phases receive the accumulated dashboard human input for the issue, including
planning clarifications, explicit overrides, approval records, and later code-review
feedback. Symphony freezes this history per run and restores it after a process
restart; each new run accumulates the additional input. The history is attached at
the shared Codex dispatch point, so plan repairs and implementation/review corrections
receive it as well. It is separate from the excluded Jira comments.

Explicit operator decisions accepted before the applicable plan approval govern the
specific points they resolve. Review must not re-raise a waived criterion merely
because the original Jira wording differs. New post-approval scope changes still
require replanning and approval. The dashboard exposes the exact accumulated context
used by each run for inspection.

Running tests is optional. `hooks.verify` defaults to null; configured trusted
checks record their result in the dashboard as advisory. Failed, unavailable, or
unconfigured checks do not block handoff or require a bypass approval. Concrete
code defects still require review corrections. Reports distinguish passed, failed,
and unrun tests. Old `verify_required` settings are normalized to false.

The dashboard shows all five workflow stages, highlights the next human action,
keeps plan/review summaries readable, and pauses refresh while reading or editing.
Existing stored data and workspaces are not deleted by startup; retired feature
columns are no longer used, and new databases create only the development schema.

## Jira comments excluded from planning

The checked-in workflow sets `tracker.requirements.include_comments: false`.
Jira comments are not fetched for the requirements package and embedded comments
are discarded. Only Description and configured Acceptance Criteria define new
planning scope. Comment-only edits or comment API failures cannot change the new
requirements hash or block planning. The prompt does not include a comment summary.
Dashboard human code-review feedback and saved review context remain available.

The configuration option defaults to true for older custom workflows. Previously
saved snapshots retain their original hashes and approval bindings; changing this
policy can require replanning a previously approved, comment-bearing snapshot.
The evidence mechanics below also describe those older snapshots.

## Canonical Jira requirements

For each hydrated issue Symphony builds a versioned requirements snapshot. The
authoritative planning boundary is deliberately small: only the root issue's
Description, configured `acceptance_criteria_fields`, and fully paginated root
comments can create requirements or acceptance criteria. Generic custom fields,
attachments, parent/child/link information, components, and versions may be retained
in the serialized snapshot as context, but they cannot create PlanSpec coverage
obligations or block planning when that context is unavailable.

The snapshot separates current requirements, superseded requirements, inferred
behavior, and unresolved contradictions. Symphony-generated status comments are
filtered before requirements are classified. Material planning evidence is
serialized canonically and SHA-256 hashed. Contextual metadata and warnings do not
change that hash. Derived Jira/source URLs, field display names, and rendered unit
locations are likewise excluded; stable issue/source IDs, evidence text,
classification, and provenance remain material. The hash is the completed-work
identity and the requirements version to which a plan is bound.

The built-in classifier supports `[classification: current]`, `[inferred]` or
`[classification: inferred]`, `[superseded]` or
`[classification: superseded]`, and `[contradiction]` or
`[classification: unresolved_contradiction]`. Use a stable target to replace an
older decision, for example:

```text
[supersedes: jira:ICPM-67703:comment:12345]
```

Only an explicit `[supersedes: ...]` reference changes its target to superseded;
comma-separate multiple targets and prefer the full
`jira:<issue-key>:<artifact-id>` decision ID. Unknown, ambiguous, and self-
references make the snapshot incomplete. Replacement prose such as “this
supersedes the previous decision” without an explicit target becomes an unresolved
contradiction and blocks planning and approval.

New snapshots use `jira-requirements/v4`. A bounded deterministic splitter keeps a
single decision's existing ID, but gives each clause or bullet in a mixed artifact
a digest-stable `#unit:<digest>` decision/source ID and location. This lets current,
superseded, and inferred bullets from one source be cited separately. Lexical
positive/negative or `before`/`after` reversals do not automatically manufacture a
hard contradiction because role and state qualifiers can make both statements
valid. Use an explicit `[contradiction]` marker for a real unresolved product
conflict.

Supersession is applied only after its target resolves unambiguously, the overriding
source authority ranks at least as high as every target, and the resulting graph is
acyclic. Lower-ranked, unranked, ambiguous, self-referential, and cyclic overrides
remain unresolved and hard-block. Stored v1-v3 snapshots remain readable with their
original canonical hash semantics. V4 makes the planning-authority boundary
explicit and excludes context from approval identity.

Configure Jira ingestion under `tracker.requirements`:

```yaml
tracker:
  requirements:
    custom_fields: ["customfield_12345"]
    acceptance_criteria_fields: ["customfield_23456"]
    field_authority:
      customfield_12345: product
      customfield_23456: product
    description_authority: product
    comment_authority: product
    comment_authority_by_author:
      product.owner@example.com: product
    authority_rank:
      context: 10
      supporting_evidence: 10
      engineering_context: 20
      product: 30
      product_owner: 40
    relation_authority: context
    comment_page_size: 100
    related_issue_hydration_max_concurrency: 8
    hydrate_search_results: true
    discover_epic_children: true
    child_issue_jql: null
    child_issue_max_pages: 100
```

`custom_fields` retains additional Jira fields as context only. Only fields in
`acceptance_criteria_fields` become acceptance-criterion planning evidence. Use Jira
field IDs, not display names. `field_authority` can override the authority of
individual acceptance-criteria fields. `comment_authority_by_author` overrides
`comment_authority` for an exact display name, email address, or Jira username;
matching trims whitespace and is case-insensitive. Unmapped authors use the
`comment_authority` fallback. For an edited comment, the represented current body
uses Jira's `updateAuthor` and `updated` time, so authority and timestamp describe
the same decision version. `authority_rank` keys are trimmed and case-insensitive,
and custom authorities are merged with the built-in ranks. Rank values must be
actual non-negative integers (booleans are rejected), and differently spelled keys
that normalize to the same authority cannot provide conflicting ranks. Every
configured description, comment, per-author, per-field, attachment, and relation
authority must be nonblank and ranked or configuration validation fails.

With `discover_epic_children: true` (the default), an Epic is searched with the
modern, quoted `parent = "<issue-key>"` JQL even when `child_issue_jql` is null.
Non-Epics do not incur that search. Set `discover_epic_children: false` to opt out,
or set `child_issue_jql` to an installation-specific template containing
`{issue_key}`; an explicit template retains the prior behavior and is used for any
issue type. Child pages are deduplicated and bounded by `child_issue_max_pages`
(default 100, maximum 1,000). A returned `startAt` must be a non-negative integer
equal to the requested offset; a mismatched page is rejected before any child on it
is accepted. Fetch errors, malformed totals/`isLast`, repeated/non-progressing
pages, early truncation, and hitting the page bound are retained as context warnings;
they do not block planning evidence from the root issue.


`symphony_comment_patterns` can replace the default regular expressions used to
exclude Symphony's own start, completion, failure, blocked, and plan-ready comments
from product requirements. Override it only when your Jira comment format differs.

Search results are hydrated by default so polling never compares a partial issue
with a previously approved full snapshot. `hydrate_search_results` must be true;
configuration validation rejects the former opt-out. Root search matches are
hydrated in fixed-size batches, and one-hop parent, child, link, and dependency
hydration uses the same `related_issue_hydration_max_concurrency` ceiling (default
8, hard maximum 32). This setting bounds work; it does not truncate issue counts.

Root comments are paginated against Jira's declared `total`, with duplicate/non-
progress protection. An early `isLast`, empty/short page, malformed response, or
request failure that leaves the declared total incomplete adds a canonical hard
incomplete reason. Any comment or changelog page that returns `startAt` must provide a real
non-negative integer equal to the requested offset; a negative, non-integer, or
jumped page is rejected before its comments or histories are accepted. Jira may
also embed only part of an expanded changelog; when its declared total exceeds
returned histories, Symphony explicitly paginates the full changelog for root and
related issues. Root comment-content truncation hard-blocks planning. Changelog and
related-issue failures are metadata/context warnings. A configured Acceptance
Criteria field omitted from the root response hard-blocks; a generic custom field or
any field omitted from a related response does not.

Presence is checked separately from value. Only omitted root `description`, root
`comment`, or configured Acceptance Criteria keys are hard evidence gaps. Null or
empty values are valid. Missing status, issue type, assignee, parent, subtasks,
links, components, versions, attachment metadata, generic custom fields, or related
fields are context warnings and do not block an otherwise complete snapshot.

Initial field provenance uses Jira `creator` and `created`; an edit uses its exact
changelog author and timestamp rather than borrowing the reporter. Missing,
malformed, or conflicting provenance is retained as an unknown/null context warning
and does not block known source content. Decision authorities remain explicit,
nonblank, and ranked because authority affects supersession precedence.

### Attachments

Attachments are intentionally excluded from planning for now. Jira attachment
metadata is retained for inspection, but normal issue ingestion does not download or
analyze attachment content. Attachment names, contents, analysis status, failures,
and markers cannot create requirements, acceptance criteria, contradictions,
PlanSpec coverage, incomplete reasons, or approval-hash changes. Put any required
behavior from a mockup into Description, Acceptance Criteria, or a root Jira comment
before running Symphony.

Low-level attachment download/analyzer helpers remain isolated for tests and future
use, but they are not part of the v4 Jira planning-evidence pipeline.

## PlanSpec, artifacts, and approval

When `codex.plan_before_implementation` is enabled, the planning pass receives the
canonical v4 planning-evidence document and its hash. Its successful output must be a
validated PlanSpec JSON object; free-form plans cannot proceed to approval or
implementation. The schema requires:

- Jira-sourced requirement and acceptance-criterion IDs with exact source citations;
- complete coverage of every current requirement and acceptance-criterion source;
- an optional role/state behavior matrix for behavior that actually varies by role
  or state;
- affected repositories, files, APIs, schemas, migrations, and translations;
- repository baseline SHAs, precedents, and the simplest implementation considered;
- non-goals and prohibited scope;
- at least one test case mapped to every acceptance criterion; and
- rollout, rollback, compatibility, risks, open questions, and Epic strategy.

Every current requirement source must appear in the PlanSpec requirement layer, and
every current acceptance-criterion source must appear in its matching nested
acceptance-criterion layer. Each citation must match the exact Jira issue identifier,
source type, and source ID in the authoritative root Description, configured
Acceptance Criteria, or root comments. Context-only sources are not valid PlanSpec
anchors. For v4 split decisions, the exact `#unit:<digest>` source ID is required; a
base artifact ID does not cover its decision units. Frozen v1-v3 verification keeps
the historical base/unit matching rule so existing approvals remain verifiable.
Role-neutral requirements and criteria do not need a matrix row; every ID that a row
does reference must exist in the same PlanSpec.

Role rows are derived only from active root Jira decisions. Context metadata and
attachments cannot create role rows. Canonical role coverage uses explicit
PlanSpec IDs and `canonical_role` values; it is not inferred by scanning free-form
role labels for words such as “GC” or “Sub”.

Each role/state row separates machine-stable identity from presentation:
`canonical_role` must be one of `gc`, `sub`, `gc_as_sub`, `all`, or `other`, while
`role` is a human-readable display label. Validation trusts `canonical_role`; it
does not infer or reject roles by scanning display text for words such as “GC” or
“Sub”.

Repository names in `baseline_repository_shas` are normalized POSIX paths relative
to the prepared workspace; use `.` for the workspace-root repository. Redundant
spellings such as `./services/api` and `services/./api` normalize to `services/api`,
and listing more than one spelling of the same repository is rejected. Symphony
resolves each path inside the workspace and checks the declared SHA against Git
before approval, implementation, and later requirements checkpoints. A missing,
non-Git, or incorrectly rooted repository, or a SHA mismatch, returns the work to
planning rather than allowing an approval to bind to different code.

Initial planning, dashboard approval, and approval-bound continuation also require
every declared worktree to be clean at its baseline SHA. Only untracked
`.symphony/**` run artifacts are ignored; tracked changes under `.symphony` remain
dirty. Every cited precedent must be Git-tracked and outside `.symphony`. Once
implementation starts, expected worktree changes are allowed, but each repository's
HEAD must remain at the approved baseline SHA through implementation and review.

Every cited Jira source must exist in the current snapshot. The canonical PlanSpec
is written to `.symphony/codex-plan.md` by default (`codex.output_plan_file`) and
has its own stable SHA-256 content hash. Other run artifacts default to:

- `.symphony/codex-final.md` — implementation result;
- `.symphony/codex-review.md` — latest independent review; and
- `.symphony/codex-review-history.md` — all review passes.

The current requirements document is stored at
`.symphony/requirements-snapshot.json`, with immutable content-addressed versions
under `.symphony/requirements-snapshots/<sha256>.json`. Reads and writes are bounded,
owner-only, atomic, no-follow, regular-file and inode-identity checked. Symphony
fails closed when the required POSIX directory-descriptor primitives are unavailable
or when a symlink, hard-link substitution, FIFO, ownership, permission, size, or
rename race makes the artifact boundary unsafe.

With `codex.require_plan_approval: true`, the dashboard requires an explicit
approval action and approver identity. An empty form submission is not approval.
SQLite records the approver identity, approval time, exact PlanSpec hash, and exact
requirements snapshot hash. Before implementation and review, Symphony fetches Jira
again and revalidates both artifacts. A material Jira change or a modified PlanSpec
invalidates the approval and returns the work to planning.

Only the latest actionable blocked run for an issue accepts dashboard input or
approval. The exact approval record and linked “Approved.” resume input are
committed atomically with a durable predecessor-to-resume-run handoff. Workers use
a renewable five-minute lease with a heartbeat no slower than 60 seconds. After a
restart, Symphony reclaims the same run, workspace, attempt, PlanSpec hash, approval,
and input lineage, refetches Jira, and revalidates the exact bindings before Codex
runs. Lease expiry alone fences the old owner; stale tokens cannot renew, update, or
finalize, and terminal updates retire their token atomically. Stale inputs are
discarded, and implementation still requires the same active persisted approver,
approval time, PlanSpec hash, and requirements snapshot hash.

Review decisions distinguish code corrections from a wrong plan. `changes_required`
is limited to code-only work within the exact validated PlanSpec.
`plan_changes_required` means required behavior, scope, requirements/acceptance
criteria, architecture, or affected surfaces make that PlanSpec wrong; Symphony
invalidates approval and blocks in planning for replan and reapproval. `approve`
continues normally. Empty or unrecognized review output is invalid and blocks; it
never defaults to approval.

## Addressing human review after completion

The dashboard exposes **Address Human Review** only on the latest completed run for
an issue. The action accepts the reviewer identity, an absolute HTTP(S) review or PR
link, and pasted comments. The equivalent API request is:

```bash
curl -X POST http://localhost:3333/api/v1/runs/<run-id>/human-review \
  -H 'Content-Type: application/json' \
  -d '{
    "reviewer_identity": "reviewer@example.com",
    "source_url": "https://github.example.com/org/repo/pull/123",
    "comments": "Please reuse the existing helper and add the missing regression test."
  }'
```

Submission atomically freezes the exact requirements snapshot, validated PlanSpec,
active approval, previous final response, review and review history, workspace path,
repository HEADs, and current tracked/untracked diff. It creates one linked queued
result run in the same workspace. The action and result run remain linked to the
source run, reviewer, and review URL in SQLite and in
`GET /api/v1/runs/<run-id>` responses. Internal lease tokens are never returned by
the dashboard API.

The polling daemon (`python3 -m symphony_jira run ./WORKFLOW.md`) dispatches queued
review actions. The standalone dashboard process records the action but does not run
Codex, so run both commands when using the UI. Completed-review dispatch does not
require Jira to remain active and does not post start/finish comments or transition
the issue again.

Before any edits, Symphony runs a read-only triage pass:

- `code_changes` resumes the retained workspace, applies only feedback within the
  exact approved PlanSpec, runs verification, and always runs the independent review
  loop again before producing a new completed run;
- `plan_changes_required` invalidates the old approval and blocks in planning before
  implementation. Behavior, scope, acceptance-criteria, architecture, compatibility,
  or affected-surface changes need a new PlanSpec and, when configured, a new exact
  approval. To continue a blocked replan from a terminal/handoff status, reopen the
  issue into an active status. If product requirements changed, update authoritative
  Jira evidence before replanning; pasted review text is never silently promoted to
  a product requirement; and
- `needs_human` or invalid triage output blocks in review without editing code.

The triage pass uses a normalized read-only Codex sandbox, skips `before_run` hooks
that could mutate the frozen diff, and rejects workspace drift before and after
triage. Trusted retained artifacts are bounded, owner-checked regular UTF-8 files;
symlinks, hard links, special files, paths outside the workspace, changed repository
HEADs, and oversized diffs fail closed.

The dashboard is unauthenticated and intended for loopback use. Keep the default
`127.0.0.1` binding unless an authenticated reverse proxy protects it.

The configured `hooks.verify` command is advisory by default. Symphony records
`passed` or `failed`, retains the hook log path, and surfaces the result in the
dashboard and finish comment. Set `hooks.verify_required: true` to make a failed
hook block the run in the verification phase; with the default `false`, Symphony
records a warning and continues to review and handoff.

Epics must choose one of two strategies in PlanSpec:

- `decomposed` partitions every requirement and acceptance criterion into bounded
  child plans, which are executed and approved independently; or
- `single_change` explains why the Epic is bounded and always requires explicit
  approval of that exact PlanSpec, even if the global approval gate is disabled.

## Optional host-side development verification

The trusted targeted verifier remains available when operators choose to run it.
Set `hooks.use_development_verification_request: true` and configure a hook:

```yaml
hooks:
  verify: |
    /home/adkuppa/Symphony-Python/python/.venv/bin/python -m symphony_jira.development_verification \
      --workspace "{{ workspace_path }}" \
      --request "{{ verification_request_path }}" \
      --sha256 "{{ verification_request_hash }}" \
      --entrypoint /home/adkuppa/Symphony-Python/python/scripts/test.sh
  use_development_verification_request: true
```

Ask implementation to write `.symphony/development-verification-request.json` with
`schema_version: "1.0"` and `targets` containing each PlanSpec repository plus its
focused `test_args`. The request cannot choose an executable or shell command.
Symphony validates the selectors and uses the fixed repository runner. Missing or
failed requests remain advisory; the dashboard retains the result and evidence.

## Commands

```bash
python3 -m symphony_jira validate ./WORKFLOW.md
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100 --dry-run
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100
python3 -m symphony_jira run ./WORKFLOW.md
python3 -m symphony_jira dashboard ./WORKFLOW.md --port 3333
```
