# Symphony Jira Python MVP

It uses Jira REST credentials from `WORKFLOW.md`, prepares a per-issue local
workspace, runs the locally installed `codex` CLI with `codex exec --json`, stores
run data in SQLite, and posts Jira comments from the Python orchestrator.

It does not require `OPENAI_API_KEY` or `CODEX_API_KEY`. Codex authentication is
whatever the local `codex` CLI already uses.

## Canonical Jira requirements

Symphony does not treat an issue description plus the currently returned comment
page as a complete specification. For each hydrated issue it builds a versioned
requirements snapshot containing:

- the description, configured custom fields, and every paginated comment;
- attachment metadata, content hashes, and OCR/vision/text analysis summaries;
- parent, child, linked, and dependency issues;
- components and affected/fix versions; and
- source ID, author, timestamp, URL, and authority metadata for requirement-bearing
  decisions.

The snapshot separates current requirements, superseded requirements, inferred
behavior, and unresolved contradictions. Symphony-generated status comments are
filtered before requirements are classified. Material content is serialized
canonically and SHA-256 hashed; capture time and analyzer execution time do not
change that hash. The hash is the completed-work identity and the requirements
version to which a plan is bound.

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

New snapshots use `jira-requirements/v2`. A bounded deterministic splitter keeps a
single decision's existing ID, but gives each clause or bullet in a mixed artifact
a digest-stable `#unit:<digest>` decision/source ID and location. This lets current,
superseded, and inferred bullets from one comment be cited separately. Clear
positive/negative or `before`/`after` reversals among otherwise-current units are
grouped as an unresolved conflict citing every source, including conflicts within
one artifact or on related issues. This is deliberately conservative lexical
reconciliation, not a claim of general semantic understanding.

Supersession is applied only after its target resolves unambiguously, the overriding
source authority ranks at least as high as every target, and the resulting graph is
acyclic. Lower-ranked, unranked, ambiguous, self-referential, and cyclic overrides
remain unresolved and hard-block. Stored v1 snapshots remain readable, while v2
prevents a previously ambiguous whole-comment citation from approving one mixed
decision artifact.

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
    attachment_authority: supporting_evidence
    relation_authority: context
    comment_page_size: 100
    related_issue_hydration_max_concurrency: 8
    download_attachments: true
    max_attachment_bytes: 10485760
    attachment_download_max_concurrency: 4
    require_attachment_analysis: true
    attachment_analyzer: codex
    attachment_analysis_timeout_seconds: 120
    attachment_pdf_max_pages: 4
    attachment_analysis_max_concurrency: 1
    attachment_analysis_max_output_characters: 12000
    hydrate_search_results: true
    discover_epic_children: true
    child_issue_jql: null
    child_issue_max_pages: 100
```

`custom_fields` adds requirement-bearing Jira fields. Fields in
`acceptance_criteria_fields` are additionally classified as acceptance-criterion
evidence. Use Jira field IDs, not display names. `field_authority` can override the
authority of individual fields. `comment_authority_by_author` overrides
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
pages, early truncation, and hitting the page bound make the snapshot incomplete.


`symphony_comment_patterns` can replace the default regular expressions used to
exclude Symphony's own start, completion, failure, blocked, and plan-ready comments
from product requirements. Override it only when your Jira comment format differs.

Search results are hydrated by default so polling never compares a partial issue
with a previously approved full snapshot. `hydrate_search_results` must be true;
configuration validation rejects the former opt-out. Root search matches are
hydrated in fixed-size batches, and one-hop parent, child, link, and dependency
hydration uses the same `related_issue_hydration_max_concurrency` ceiling (default
8, hard maximum 32). This setting bounds work; it does not truncate issue counts.

Comments are paginated against Jira's declared `total`, with duplicate/non-progress
protection. An early `isLast`, empty/short page, malformed response, or request
failure that leaves the declared total incomplete adds a canonical incomplete
reason. Any comment or changelog page that returns `startAt` must provide a real
non-negative integer equal to the requested offset; a negative, non-integer, or
jumped page is rejected before its comments or histories are accepted. Jira may
also embed only part of an expanded changelog; when its declared total exceeds
returned histories, Symphony explicitly paginates the full changelog for root and
related issues. Failed or short comment/changelog provenance hard-blocks planning
and approval. A configured custom or acceptance-criteria field key that is absent
from a related response also blocks; an explicitly present null value remains valid
empty evidence.

Presence is checked separately from value. A root response must contain every
requested base key: `summary`, `description`, `status`, `priority`, `issuetype`,
`assignee`, `reporter`, `creator`, `labels`, `created`, `updated`, `comment`,
`attachment`, `parent`, `subtasks`, `issuelinks`, `components`, `versions`, and
`fixVersions`. Related responses must contain `summary`, `description`, `status`,
`issuetype`, `creator`, `created`, `attachment`, and every configured requirement
field. Null or empty values are valid; omitted keys are incomplete. This means a
missing Epic `issuetype` cannot silently suppress default child discovery and still
produce an approvable snapshot.

An absent or non-object changelog, a missing/invalid total, malformed histories, or
a total that does not match the complete history set is also incomplete. Initial
field provenance uses Jira `creator` and `created`; an edit uses its exact changelog
author and timestamp and fails closed rather than borrowing the reporter. Conflicting
same-timestamp author or target-value candidates resolve to unknown author/time in a
payload-order-independent way. Every decision source must have a known author,
timestamp, and nonblank ranked authority.

### Attachments and incomplete evidence

Attachment content URLs are resolved against `tracker.base_url`. Relative URLs are
allowed, but an absolute or protocol-relative URL is fetched only when its scheme,
host, and effective port exactly match the configured Jira origin; off-origin URLs
are rejected before any request, so Jira authorization is never sent to another
host. Downloads are streamed. Jira metadata and HTTP `Content-Length` are checked
before reading, and streaming stops immediately once `max_attachment_bytes` would
be exceeded, so the adapter never buffers an unbounded response. Per-list chunks
and one JiraClient-wide semaphore, controlled by
`attachment_download_max_concurrency` (default 4, hard maximum 32), cover the full
download-and-analysis operation. Concurrent root, search-result, and related-issue
attachment lists therefore share the same ceiling. This is separate from the
analyzer subprocess concurrency limit.

`attachment_analyzer: basic` extracts bounded UTF-8 text from text, JSON, CSV, XML,
YAML, SQL, and similar files; binary evidence remains explicitly incomplete.
`attachment_analyzer: codex` preserves that text behavior and uses the locally
authenticated `codex` CLI to summarize PNG, JPEG, and WebP screenshots. It does not
require a separate API key. PDF evidence is rendered locally with `pdftoppm`, up to
`attachment_pdf_max_pages`, and the renderer probes one additional page so it can
reject rather than silently truncate a longer PDF. Animated GIFs and other formats
that cannot be completely inspected are marked unsupported.

Codex mode sends each screenshot and rendered PDF page to whichever model provider
the locally authenticated Codex CLI is configured to use; that provider may be
remote even though Symphony performs the download, rendering, and cache management
locally. Before enabling Codex mode, operators must confirm that sending the Jira
evidence to that provider is approved under their data-classification, retention,
and handling requirements.

The vision prompt treats attachment content as untrusted evidence and tells Codex
to ignore embedded instructions. Each subprocess is ephemeral, ignores repository
rules, skips Git discovery, runs in a private temporary directory with a read-only
sandbox, and has a configured timeout. `attachment_analysis_max_concurrency`
limits simultaneous OCR/vision subprocesses (the default is one), while
`attachment_analysis_max_output_characters` bounds stored summaries. Oversized,
disabled, unsupported, timed-out, or failed evidence is retained with an explicit
non-complete status.
The vision contract prefixes directly visible observation bullets with
`[classification: current]`, interpretations with `[inferred]`, and explicit
within-attachment conflicts with `[contradiction]`; it does not claim conflicts
against unseen Jira sources. Only complete, nonblank analysis summaries become
decision units in the normal current/superseded/inferred/contradiction taxonomy.
They use the attachment's exact root or related-issue provenance and
`supporting_evidence` kind, so current exact units may anchor PlanRequirements while
inference/ambiguity notes do not become mandatory scope or manufacture acceptance
criteria from an “Acceptance evidence” label.

Successful summaries are cached at
`.symphony/attachment-analysis-cache/<content-sha256>.json`; attachment bytes are
never persisted there. The cache directory and files use private permissions,
writes are atomic, and reads reject symlinks, non-regular entries, wrong ownership,
or group/world-accessible permissions. Entries are bound to the content hash,
analyzer-contract hash, analyzer ID, and resolved analyzer-engine identity.
Changing the prompt contract, a result-affecting bound, or the Codex executable or
version therefore refreshes the summary. Errors and incomplete analyses are not
cached, so a later poll can retry after local tooling is repaired.

With `require_attachment_analysis: true`, any attachment without a complete
analysis adds an `incomplete_reasons` entry to the snapshot. Unknown source authors,
missing source timestamps, bad supersession references, and unresolved
contradictions are also explicit incomplete reasons. Any snapshot incomplete reason
is a hard gate before planning and implementation: correct the Jira provenance or
evidence, then refetch the issue. Dashboard clarification text cannot waive an
unresolved Jira contradiction because it has no Jira source provenance. Setting
`require_attachment_analysis: false` before snapshot creation is the explicit
workflow-level choice to accept metadata-only, skipped, or unsupported attachments;
it does not waive any other incomplete reason. Attachment download/security errors,
including an off-origin content URL, remain incomplete even when analysis is
optional.

## PlanSpec, artifacts, and approval

When `codex.plan_before_implementation` is enabled, the planning pass receives the
full canonical requirements snapshot and its hash. Its successful output must be a
validated PlanSpec JSON object; free-form plans cannot proceed to approval or
implementation. The schema requires:

- Jira-sourced requirement and acceptance-criterion IDs with exact source citations;
- complete coverage of every current requirement and acceptance-criterion source;
- a role/state behavior matrix that references every planned requirement and
  acceptance-criterion ID;
- affected repositories, files, APIs, schemas, migrations, and translations;
- repository baseline SHAs, precedents, and the simplest implementation considered;
- non-goals and prohibited scope;
- exactly one test case mapped to every acceptance criterion; and
- rollout, rollback, compatibility, risks, open questions, and Epic strategy.

Every current requirement source must appear in the PlanSpec requirement layer, and
every current acceptance-criterion source must appear in its matching nested
acceptance-criterion layer. Each citation must match the exact Jira issue identifier,
source type, and source ID in the snapshot, including evidence from related-issue
attachments. Every planned requirement and acceptance-criterion ID must appear in
at least one role/state matrix row.

Every completely analyzed root or related-issue attachment must be cited by active
scope; citing one of its exact `#unit` decision sources also covers the base
attachment without a duplicate citation. A PlanRequirement may use a current
attachment `supporting_evidence` unit as its current Jira anchor, which permits
screenshot-only placement behavior. AcceptanceCriteria remain strict: they require
an explicit current `acceptance_criterion` decision and are never inferred from an
attachment section label. Superseded, inferred, or contradictory evidence cannot be
promoted into active scope merely by citing it.

Role rows are required for canonical roles named in current decisions and complete
attachment summaries. A summary saying a role is absent, hidden, omitted, not
shown, or not applicable does not create a row by itself; an explicit current Jira
decision that the role is not applicable still requires a row documenting that
state.

Each role/state row separates machine-stable identity from presentation:
`canonical_role` must be one of `gc`, `sub`, `gc_as_sub`, `all`, or `other`, while
`role` is the human-readable display label. GC, Sub, and GC-as-Sub rows must use a
non-negated label that identifies only the matching canonical role; `all` and
`other` labels cannot masquerade as one of those role-specific rows. Coverage is
validated by `canonical_role`, not by freely varying display text.

Repository names in `baseline_repository_shas` are paths relative to the prepared
workspace; use `.` for the workspace-root repository. Symphony resolves each path
inside the workspace and checks the declared SHA against Git before approval,
implementation, and later requirements checkpoints. A missing, non-Git, or
incorrectly rooted repository, or a SHA mismatch, returns the work to planning
rather than allowing an approval to bind to different code.

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

The configured `hooks.verify` command is advisory. Symphony records `passed` or
`failed`, retains the hook log path, and surfaces the result in the dashboard and
finish comment. A failed command does not block review, completion, or Jira handoff;
operators can inspect the recorded result and run stack-specific checks elsewhere.

Epics must choose one of two strategies in PlanSpec:

- `decomposed` partitions every requirement and acceptance criterion into bounded
  child plans, which are executed and approved independently; or
- `single_change` explains why the Epic is bounded and always requires explicit
  approval of that exact PlanSpec, even if the global approval gate is disabled.

## Commands

```bash
python3 -m symphony_jira validate ./WORKFLOW.md
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100 --dry-run
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100
python3 -m symphony_jira run ./WORKFLOW.md
python3 -m symphony_jira dashboard ./WORKFLOW.md --port 3333
```
