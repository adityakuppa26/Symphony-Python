# Symphony Jira — Python workflow guide

It uses Jira REST credentials from `WORKFLOW.md`, prepares a per-issue local
workspace, runs the locally installed `codex` CLI with `codex exec --json`, stores
run data in SQLite, and posts Jira comments from the Python orchestrator.

It does not require `OPENAI_API_KEY` or `CODEX_API_KEY`. Codex authentication is
whatever the local `codex` CLI already uses.

## Development workflow

The dashboard displays a 2–4 sentence business overview below each issue key.
With `codex.summarize_requirements_before_planning: true`, a short read-only Codex
call generates it from the allowed Jira requirements before planning starts.
Summaries are cached by issue and requirements snapshot, remain separate from the
approved plan, and do not add a workflow phase. Generation has a 30-second default
limit (`business_summary_timeout_seconds`); failures do not block planning.
The existing dashboard polling updates summaries without disturbing feedback forms.

`WORKFLOW.md` is the single supported workflow. Symphony inspects Jira requirements
and the relevant application code, plans the smallest correct change using existing
patterns, waits for human plan approval, implements, runs an independent code review
and correction loop, and hands off. Changes to approved scope require a new plan
and approval.

Every development agent pass receives the shared existing-pattern and minimal-change
policy, including repairs, review corrections, test selection, and post-handoff
human-review triage. Plans must identify the existing code path to extend. Changes
follow the closest applicable component/module convention and preserve behavior
outside the requirement. New abstractions, dependencies, configuration, flags,
fallbacks, retries, caching, and unrelated cleanup require a concrete necessity
within approved scope. Review findings must cite the changed location and relevant
precedent or requirement, then request the smallest correction. Routine coding
choices follow existing patterns without another approval request.

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

The checked-in workflow requests and executes focused tests after each implementation
pass. Results remain advisory; set `hooks.verify: null` to disable execution. Failed, unavailable, or
unconfigured checks do not block handoff or require a bypass approval. Concrete
code defects still require review corrections. Reports distinguish passed, failed,
and unrun tests. Old `verify_required` settings are normalized to false.
Runtime startup failures are reported as environment errors, with the original
setup exit code retained, because tests have not run. A failed Codex turn cannot
count as a completed implementation even if the CLI exits with status zero;
structured error messages are retained for diagnosis and approval routing.

Host verification reports bind the exact selected commands and code snapshot.
Symphony checks every target, exit code, skip reason, and aggregate status before
accepting a result. Missing or contradictory evidence becomes an environment error,
never a passing check. Each review correction receives the prior implementation
report and host output, followed by fresh focused test selection and execution.

Unattended execution has two separate limits: `agent.timeout_seconds` caps the
whole agent pass, while `codex.stall_timeout_seconds` stops a child that emits no
stdout or stderr for 900 seconds by default. Set the latter to `0` to disable it
for tools that legitimately remain silent longer. Host verification has its own
timeout and is not subject to this inactivity watchdog.

After `codex.max_review_no_progress_passes` consecutive reviewed implementations
without code changes (default 2), Symphony stops with the retained changes and
review findings available for inspection. It also stops before starting a correction
when the review budget cannot cover another review. Neither limit invalidates an
otherwise valid approval or discards the workspace.

Retry attempts and backoff survive polling-process restarts, including dispatch
failures before a run record exists. Exhausted or non-retryable failures do not
restart indefinitely for unchanged requirements. Revised requirements start a new
budget; an intentional `once --issue <KEY>` execution remains available after
fixing the cause. Existing queued/running records prevent duplicate dispatch; this
does not automatically reclaim orphaned runs after a hard process kill. Durable
human resumes continue to use their existing lease recovery.

Jira search outages no longer skip reconciliation of active issues. On normal stop,
Ctrl-C, or SIGTERM, the polling process cancels and waits for its workers so their
process cleanup, workspace hooks, and final run records settle before shutdown.

The dashboard shows the development stages and the host-test phase, highlights the next human action,
keeps plan/review summaries readable, and pauses refresh while reading or editing.
Use **Enable sound** in each dashboard tab to hear a two-tone alert when a new
human-input request arrives; the volume control is remembered in this browser.
Desktop notifications are optional and require browser permission. Alerts check
every 15 seconds even while page refresh is paused, with duplicates suppressed
for requests already notified. Keep the dashboard open; browser background-tab
throttling can delay checks. Use **Load latest cases** to refresh the rows when ready.
Case search and status filters help find approvals, active work, and handoffs.
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

Before finalizing a plan, Codex is instructed to read relevant links in Jira
Description and Acceptance Criteria through its available MCP tools, including
Confluence, and visually inspect embedded/linked UI mockups and relevant issue
mockup attachments. It records source URLs, versions when available, image
identifiers, and relevant observations in the existing PlanSpec fields. The
approved plan carries that interpretation into implementation and review.
Unavailable required evidence prompts a specific human question; incidental gaps
are recorded as risks. Jira comments remain excluded.

This uses Codex's existing MCP access; host ingestion does not download external
pages/images or include their contents in the Jira snapshot hash. External changes
are not automatically detected. Plan citations still use the referring root Jira
source IDs. Unreferenced attachments do not independently create scope.

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
an issue. Paste a batch of comments, including file names, line references, and code
examples. The reviewer field starts as `Operator` and can be changed to the actual
reviewer's name. A review/PR URL is optional; when provided it must be an absolute
HTTP(S) URL. No MCP connector is needed for manual feedback.

The form previews the exact text and saves drafts in session storage for this
browser tab. Reloading restores the draft; closing the tab ends that storage.
**Clear draft** removes it. Submission errors appear inline and retain the input.
Repeated identical submissions for the same handoff return the existing review and
result run, including after a lost response or process restart. A different batch
cannot overwrite one already submitted. Submit the next round from its completed
result run.

After submission, **Open review progress & response** opens `/reviews/<review-id>`.
It shows the scope check's comment list, current status, verification result, and
the implementation's response without refreshing away your reading position.
The read-only triage assigns comment IDs; implementation is instructed to respond
to each with its resolution and file/check evidence, and independent review checks
those claims against the diff. An item in the scope checklist is proposed work,
not proof of completion.

The checked-in workflow enables `codex.require_review_comment_coverage`. Before
implementation, Symphony validates the triage checklist's IDs, text, scope, and
proposed actions. Mixed-scope items cannot be routed as an in-scope batch. An
independent reviewer may approve only with a `comment_resolutions` entry for every
checklist ID, exactly once, giving `addressed`, `already_satisfied`, or `unresolved`
and nonempty code/check evidence. Unresolved items return to correction. Missing
or malformed checklist/resolution output receives one read-only repair attempt;
continued invalid output blocks handoff. Successful resolution evidence is stored
with the reviewed workspace hash and shown on the progress page after completion.
Older custom workflows keep their previous behavior unless this option is enabled;
historical results without evidence are labeled `not recorded`.

These checks establish complete, structured review records. They do not prove the
reviewer's semantic judgment is correct: independent review must still inspect the
original comments, actual code, tests, and accepted human decisions. Test execution
remains advisory, and unrun checks cannot be cited as passing evidence.

Each implementation pass also compares snapshots of every managed repository.
Changing a repository outside the approved PlanSpec stops handoff and preserves
the edits for inspection. Unchanged pre-existing work in other repositories is
allowed. This boundary supplements the existing code-pattern and minimal-change
instructions; it does not infer whether every edit within an approved repository
is justified.

Run the focused handoff acceptance scenarios with
`.venv/bin/python -m pytest -q tests/test_review_completion.py` from `python/`.
They exercise repair bounds, omitted/duplicate comment dispositions, unresolved
feedback, scope conflicts, read-only review mutations, and repository boundaries.
These are deterministic orchestration tests using controlled runner responses,
not a benchmark of live model quality.

The equivalent API request is:

```bash
curl -X POST http://localhost:3333/api/v1/runs/<run-id>/human-review \
  -H 'Content-Type: application/json' \
  -d '{
    "reviewer_identity": "reviewer@example.com",
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
- `needs_human` blocks in review without editing code. Answer the scope question
  directly on the review progress page using **Save answer & resume review**.
  Symphony reruns the read-only scope check on the same saved review batch, even
  while Jira remains Done. Answers are durable, stay separate from the original
  pasted comments, and reach subsequent implementation/review phases and later
  review rounds. Workspace and approval integrity checks still apply. Answer
  submissions carry the displayed question revision so a stale tab cannot replace
  a newer answer; retries of the same answer are idempotent; and
- invalid triage output blocks in review without editing code and is not eligible
  for the scope-answer shortcut.

`GET /api/v1/reviews/<review-id>` supplies lightweight progress, comment items,
answers, and the latest response. `POST /api/v1/reviews/<review-id>/clarifications`
accepts `response` plus the `expected_revision` from the progress response's
`clarification_revision`. Future MCP ingestion can submit normalized reviewer,
optional source URL, and comments through the existing submission endpoint; the
frozen-context, triage, and review pipeline is shared. No external comments are
fetched, posted, or marked resolved by this manual flow.

The triage pass uses a normalized read-only Codex sandbox, skips `before_run` hooks
that could mutate the frozen diff, and rejects workspace drift before and after
triage. Trusted retained artifacts are bounded, owner-checked regular UTF-8 files;
symlinks, hard links, special files, paths outside the workspace, changed repository
HEADs, and oversized diffs fail closed.

The dashboard is unauthenticated and intended for loopback use. Keep the default
`127.0.0.1` binding unless an authenticated reverse proxy protects it.

The configured host verification is advisory. Results distinguish passed tests,
failed tests, runtime/environment errors, skipped or partial coverage, and stale
results when the code changed. There is no human bypass gate.

Epics must choose one of two strategies in PlanSpec:

- `decomposed` partitions every requirement and acceptance criterion into bounded
  child plans, which are executed and approved independently; or
- `single_change` explains why the Epic is bounded and always requires explicit
  approval of that exact PlanSpec, even if the global approval gate is disabled.

## Change-aware host verification

The workflow is planning → human approval → implementation → test selection →
host runtime/tests → code review. A correction pass repeats selection and testing
against its new diff. All Codex phases receive the accumulated human decisions.

`codex.select_tests_after_implementation: true` enables a read-only selection pass.
It receives the approved PlanSpec, implementation report, and actual workspace diff
(including staged and untracked changes). Codex returns focused test selectors and
reasons, or explicit coverage gaps. Symphony adds the workspace code hash and freezes
`.symphony/development-verification-request.json`. The host checks this hash before
and after execution; it cannot report stale results as passing.

`hooks.verify` invokes `symphony_jira.development_verification` with
`--prepare-with-test`. CPM unit/functional targets use the dedicated schema controller.
Foyr Python/frontend targets use the standalone Foyr controller. Pi retains
`scripts/test.sh`. Test processes run on the host's Podman runtime,
outside Codex's sandbox.

The checked-in workflow enables `codex.development_test_suites: [python, functional, frontend]`.

| Repository | Suite and supported selection | Runner |
| --- | --- | --- |
| CPM | `python`: `Test/unit/<file>.py`, optionally `::Class::test` | Dedicated schema stack |
| CPM | `functional`: `Test/functional/<file>.py`, optionally `::Class::test` | Dedicated schema stack |
| Foyr | `python`: `tests/<file>.py`, optionally `::Class::test` | Disposable Foyr runner |
| Foyr | `frontend`: source files under `foyr/client_src/karma_test/` or `foyr/src/` (`.spec.ts` / `.spec.tsx`) | Karma or Jest, one worker |

Symphony runs tests relevant to the actual feature-branch diff, with no automatic
sanity profile. Before freezing the request, the host discovers added and modified
Python and supported Foyr frontend test files against the approved baseline, including staged and untracked files.
Supported test files are included in full so every newly introduced case runs; filters
and early-stop flags are removed from those targets. Existing unchanged tests are
selected only for relevant behavior. Missing or unsupported coverage remains explicit
in skipped entries; unrelated checks are never substituted. Functional directory and
full-suite requests are rejected.
API suites remain out of scope until separate iBIS verification is configured.

`--cpm-entrypoint /home/adkuppa/playground/cpm-verification/verify.py` routes CPM
unit and functional targets directly to the standalone controller, bypassing
Compost startup and application containers. It receives this issue's `cpm/` checkout,
literal test selectors and validated pytest filters, and the remaining host time budget.
The controller snapshots source, resets only its owned schema in the existing Oracle
service and its private cache, and runs a disposable test container. A missing controller
is an environment error; there is no fallback to the shared CPM application runner.

Each target's result enters the normal code-bound verification report and review input.
The hook log includes paths to the controller's logs, JUnit XML, source fingerprints,
and summaries under `/home/adkuppa/playground/cpm-verification/results/`. Correction
and human-review implementation passes repeat selection and execution. Verification
remains advisory. There are no automatic smoke or sanity profiles.

For example, manual use against an issue checkout:

```sh
python3 /home/adkuppa/playground/cpm-verification/verify.py unit --source /path/to/issue/cpm Test/unit/path/test_changed_feature.py
```

Foyr uses `/home/adkuppa/playground/cpm-verification/foyr.py`. Python tests run
with `tests/testing.yml`, in-process clients, mock cache, one worker, and container
networking disabled. Neither Python nor frontend verification starts Oracle,
memcached, CPM, iBIS, or the Foyr service. No Compose command is used.

Frontend verification caches `npm ci --ignore-scripts` dependencies by both lockfiles
and the image ID. It builds the branch's assets in a private source copy, runs only
selected legacy JavaScript / TypeScript Karma files or JSX Jest files, and collects
JUnit or Jest JSON evidence. Karma uses a temporary headless browser and a loopback
server; builds and CDN assets may require network access. The runner is capped at
1.5 GiB and two CPUs, and the temporary browser at 768 MiB and one CPU. Verification
jobs share a lock with CPM to keep resource use bounded. No application services persist.

The original application image is unchanged; a small `foyr-verification` image adds
the repository's pinned Python test extras. Changes to those extras require an image
rebuild. Missing dependencies, zero tests, all-skipped results, absent evidence, and
timeouts cannot produce a passing report. Logs, source/image fingerprints, and frozen
harness files are retained alongside results.

Both `scripts/test.sh cpm` and `scripts/test.sh foyr2` reject the old Compose route
before touching services. There is no fallback from either standalone runner. Pi's
existing runtime preparation and ownership-based cleanup remain available.

```sh
python3 /home/adkuppa/playground/cpm-verification/foyr.py python --source /path/to/issue/foyr2 tests/views/api/test_home.py
python3 /home/adkuppa/playground/cpm-verification/foyr.py frontend --source /path/to/issue/foyr2 foyr/client_src/karma_test/cpm/home/homeTest.js
```

Changing application/test source to resolve real test failures remains part of the
implementation/review loop. Codex is not asked to debug container startup, package
installation, sandbox networking, or shared environment configuration.

## Commands

```bash
python3 -m symphony_jira validate ./WORKFLOW.md
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100 --dry-run
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100
python3 -m symphony_jira run ./WORKFLOW.md
python3 -m symphony_jira dashboard ./WORKFLOW.md --port 3333
```


### Human conversation and automatic replanning

The dashboard separates **Ask Codex** (a read-only answer, with no resume or
approval) from **Record decision & resume**. Questions and answers remain visible
across recent case attempts. Decisions retain the exact question they answer;
short assent is not blanket approval. The decision ledger shows newest inputs
first, while every phase still receives the full chronological history.

With `codex.auto_replan: true` (enabled in WORKFLOW.md), a structured
`plan_changes_required` response queues a durable planning continuation. It does
not authorize implementation: the revised plan still requires exact approval.
Repeated routing, or two automatic revisions without another approval, pauses
for inspection. System routing entries are never treated as operator decisions.

Validated plans are retained under `.symphony/plan-history/`. The dashboard shows
changed plan sections and brief details when a previous version is available.
PRD section headings are excluded from decision citations; explicit background,
persona, and candidate sections remain supporting evidence, while acceptance
criteria inside Description are classified at the acceptance-criterion layer.
Unrecognized sections remain requirements conservatively. Changes to this
classification affect the requirements hash and may require a fresh plan/approval.

Restart the orchestrator and dashboard to load Python changes. Existing code,
operator decisions, and verification deferrals are preserved. Test verification
remains advisory and never becomes a new approval requirement.


Derived plan acceptance criteria may cite a current authoritative Jira requirement
source also cited by their parent PlanRequirement, or an explicit current Jira
acceptance-criterion source. Every explicit Jira requirement and acceptance
criterion still requires coverage in its own layer. Supporting evidence alone or
an unrelated requirement cannot anchor a derived criterion. Citation validation
establishes traceability; planning and review must separately check that limits,
defaults, and other behavior are supported and preserve recorded human overrides
and verification deferrals.

## Implementation continuation and grouped planning questions

Implementation can return `{"implementation_status":"continue","summary":"...",
"remaining_work":["..."]}`. Symphony continues under the approved plan before
running verification or independent review. A complete report uses
`implementation_status: complete` with an empty `remaining_work` list. Older prose
reports remain compatible; explicit partial-implementation reports also continue.
Two consecutive implementation passes without code progress stop the run, as does
`codex.max_implementation_continuations` (default 6 per review iteration). Existing
approval and requirements checks still run on continuation. Test unavailability
alone does not make source implementation incomplete.

Planning investigates authorization, field sources, null/default behavior,
persistence, and UI precedents before asking for clarification. A `needs_human`
response can contain a `questions` array of strings; every question is displayed
in one numbered request instead of dropping all but the first. This reduces
avoidable clarification rounds without inventing answers to product decisions.

## Local Projects smoke checks

The old local Projects smoke scripts are diagnostic utilities, not part of the
checked-in verification workflow. They require an already-running application stack;
the new Foyr test runner does not invoke them. Real browser end-to-end tests against
running services are outside this lightweight unit/component-test stack.

### Runtime cleanup after verification

The retained Pi Compose runner releases containers it created or replaced on exit, including
failed setup and failed tests, while still holding the Compost lock. Successful
`--prepare-only` calls deliberately retain their services until `runtime.sh down`;
the workflow's `after_run` cleanup remains a fallback. Running developer-owned
services that were merely reused are preserved. A pre-existing stopped container
started for a test is stopped again; newly created containers are removed without
removing volumes.

Podman subprocesses do not inherit the lock descriptor: long-lived conmon and
network helpers must not keep verification locked after the runner exits.
Symphony records networks it creates, gives them Docker Compose-compatible labels,
and removes them only when no containers (including stopped developer containers)
use them. Cleanup also clears the stale CNI DNS helper for a removed network.
Networks that predate Symphony or have since been replaced are preserved.

Oracle cleanup first executes `SHUTDOWN IMMEDIATE` through OS-authenticated SQLPlus,
then stops and removes the container. It does not use force removal or Podman's
negative stop timeout. If database shutdown fails, cleanup reports the failure and
retains ownership for retry instead of force-killing the database. Data volumes
are never removed by this cleanup.
