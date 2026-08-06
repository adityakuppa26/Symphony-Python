# Symphony Jira Python MVP

It uses Jira REST credentials from `WORKFLOW.md`, prepares a per-issue local
workspace, runs the locally installed `codex` CLI with `codex exec --json`, stores
run data in SQLite, and posts Jira comments from the Python orchestrator.

It does not require `OPENAI_API_KEY` or `CODEX_API_KEY`. Codex authentication is
whatever the local `codex` CLI already uses.

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
`automation_plan_changes_required` retains that approved development PlanSpec and
reruns only the derived post-development automation planning/update lane.
`plan_changes_required` means required behavior, scope, requirements/acceptance
criteria, architecture, or affected surfaces make that PlanSpec wrong; Symphony
invalidates approval and blocks in planning for replan and reapproval. `approve`
continues normally. Empty or unrecognized review output is invalid and blocks; it
never defaults to approval.

## Post-development automation updates

The optional `automation` phase extends the existing flow without adding another
approval gate:

1. Symphony captures canonical Jira requirements and validates the development
   PlanSpec through the existing planning and approval flow.
2. Codex implements the approved development change in the development repositories.
3. A separate read-only planning pass compares those requirements and that exact
   PlanSpec with the development result and actual code diff, then inspects the
   existing automation repository.
4. Codex applies the focused automation plan only in the automation checkout, or
   records a justified no-op when no automation-code update is relevant.
5. The configured verification hook runs across the prepared checkouts. In the
   checked-in workflow it is required and runs `git diff --check` for the development
   and automation repositories.
6. Configured runtime verification runs only for development repositories named by
   the exact development PlanSpec; the automation checkout is deliberately not a
   runtime mapping.
7. The independent review receives both exact plans and the combined workspace
   changes. An approved run then reports completion and performs the configured Jira
   handoff.

The development PlanSpec approval remains the single human approval boundary. The
automation plan is derived from that approved contract and the resulting development
change; it cannot expand product behavior or silently edit a development repository.
An explicit no-op is a successful automation outcome, not a reason to manufacture
tests, broad cleanup, or speculative coverage.

The `verification` commands listed in an AutomationPlan are declarative plan content.
Codex is asked to run a bounded listed check when it is locally available and report
the result, but Symphony does not dispatch those model-authored command strings.
Only trusted `hooks.verify` and configured runtime profiles are orchestrator-executed;
in the checked-in workflow, the required hook covers automation with
`git diff --check`, while runtime profiles remain development-only. The dashboard
shows automation planning and implementation phases plus concise plan/result
artifacts alongside each run.

Automation is disabled by default for backwards compatibility. Configure it at the
top level of `WORKFLOW.md`:

```yaml
automation:
  enabled: true
  workspace_subdir: automation
  planning_prompt: |
    Plan the smallest relevant automation update from the canonical requirements,
    approved development PlanSpec, development result, and actual development diff.
    If no automation change is relevant, return a justified no-op. Do not edit files.
  implementation_prompt: |
    Apply the automation plan only in automation/. If it is a no-op, leave the
    checkout unchanged and report why.
  output_plan_file: .symphony/codex-automation-plan.md
  output_result_file: .symphony/codex-automation-final.md
```

`workspace_subdir`, `output_plan_file`, and `output_result_file` must be non-empty,
safe workspace-relative POSIX paths; absolute paths, parent traversal, backslashes,
and the workspace root itself are rejected. When automation is enabled, its plan and
result paths must be distinct, outside the automation checkout, and different from
every configured Codex artifact path. The checkout cannot use the reserved
`.symphony/` tree or overlap a runtime repository. Both prompts must be nonblank.
The checked-in workflow enables this phase and prepares `automation/` from
`/home/adkuppa/CPM` at `master` on a branch named exactly for the Jira key. The clone
copies Git objects instead of hard-linking them and removes the local source checkout
as a Git remote, so workspace Git operations cannot write through to the uppercase
source repository. Symphony rejects an automation checkout with any configured Git
remote. Its hook also bootstraps that checkout for retained workspaces
created before automation was enabled, without replacing any existing path, and
removes a retained local-source origin when present. The plan and result artifacts
remain under the workspace-level `.symphony/` directory so the automation repository
stays limited to relevant source changes.

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

## Local runtime verification

Symphony can verify a changed checkout or start it for manual inspection through a
host-owned Podman Compose runtime. The runtime is configured in `WORKFLOW.md`, but
Compose remains the owner of the service definitions, images, ports, and volumes.
For example:

```yaml
runtime:
  enabled: true
  required: true
  shutdown_after_handoff: true
  shutdown_grace_seconds: 120
  command: ["/usr/bin/podman", "compose"]
  project_directory: "/path/to/compost"
  compose_file: "/path/to/compost/docker-compose.yml"
  env_file: "/path/to/compost/.env"
  project_name: compost
  lock_file: "~/.local/state/symphony/compost.lock"
  lock_timeout_seconds: 900
  preview_timeout_seconds: 900
  repositories:
    cpm:
      workspace_subdir: cpm
      source_env: CPM_SRC
      service: cpm
      mount_target: /TexturaWD/textura
      dependencies: [oracledb19, memcached]
      container_workdir: /TexturaWD/textura
      verification_profile: cpm_pytest
  verification_profiles:
    cpm_pytest:
      argv: ["pytest"]
      default_args: ["Test/unit"]
      environment: {}
      timeout_seconds: 3600
```

Each repository maps a workspace subdirectory to the environment variable and
container mount already used by Compose. For every invocation, Symphony overlays
those source variables in the child process environment, renders the effective
Compose configuration, and confirms that the configured service mount resolves to
the selected workspace checkout before it starts anything. It never rewrites the
runtime `.env` file. The profile command is fixed in trusted workflow configuration;
provided `--target-arg` values replace the profile's `default_args` and are appended
to its fixed `argv`. Verification force-recreates the configured workspace-bearing
dependencies and target service together, waits for Compose health checks, and then
uses `compose exec -T` to run the profile inside the target service.
For a dependency that bind-mounts a replaceable workspace checkout, list it in
the repository's `force_recreate_dependencies` as well as `dependencies`; Symphony
includes it immediately before the target service in the same force-recreate call,
while Compose reuses transitive database and cache dependencies. Preview `start`
and `stop` reject `--target-arg`.

The machine-wide `lock_file` serializes verification, preview, and shutdown
operations because a shared Compose project can have fixed container names, ports,
networks, and volumes. This is a single runtime lane, not per-issue Compose
isolation. Commands, bounded output, and errors are recorded under
`<workspace>/.symphony/runtime/`.

Runtime subprocesses do not inherit the configured Jira credential variables or
standard `JIRA_*`, `*_JIRA_TOKEN`, and `*_JIRA_EMAIL` variables. Retained runtime
logs and manifests are owner-only, no-follow files; bounded logs preserve the most
recent command evidence with an explicit truncation marker. Verification manifests
bind the exact bytes of both the hook log and every runtime-check log by SHA-256.

Operators can exercise the same runtime without Jira credentials or a Codex
preflight:

```bash
python3 -m symphony_jira runtime ./WORKFLOW.md verify \
  --workspace /path/to/codex-workspaces/ICPM-73100 \
  --repository cpm

python3 -m symphony_jira runtime ./WORKFLOW.md verify \
  --workspace /path/to/codex-workspaces/ICPM-73100 \
  --repository cpm \
  --source-repository cpm \
  --source-repository foyr2 \
  --target-arg Test/unit

python3 -m symphony_jira runtime ./WORKFLOW.md start \
  --workspace /path/to/codex-workspaces/ICPM-73100 \
  --repository cpm

python3 -m symphony_jira runtime ./WORKFLOW.md stop \
  --workspace /path/to/codex-workspaces/ICPM-73100 \
  --repository cpm

python3 -m symphony_jira runtime ./WORKFLOW.md shutdown \
  --workspace /path/to/codex-workspaces/ICPM-73100 \
  --repository cpm \
  --repository foyr2
```

When `--source-repository` is omitted, all configured repository source variables
are bound to their checkouts in the selected workspace. Repeat it to choose an
explicit subset. Repeat `--target-arg` for multiple verification arguments; use the
`--target-arg=-q` form when an argument begins with `-`.

The command emits one compact JSON result. Verification reports `passed`,
`test_failed`, or `environment_blocked`; preview actions report `started`, `stopped`,
or `environment_blocked`; and shutdown reports `stopped` or
`environment_blocked`. Only `passed`, `started`, and `stopped` exit successfully for
their corresponding action. An environment block means the runtime could not be
safely configured or executed, while a test failure means the configured test
process ran and failed. `stop` remains a target-only preview operation for exactly
one `--repository`; `shutdown` accepts repeated repositories and includes their
configured/transitive Compose dependency closure.

On Ubuntu hosts using Podman's legacy CNI `dnsname` plugin, an error containing
`cni plugin dnsname failed: permission denied` can mean AppArmor blocked the
plugin's SIGHUP reload of `dnsmasq`. Keep the exception signal-specific: add
`signal (receive) set=(hup) peer=podman,` to
`/etc/apparmor.d/local/usr.sbin.dnsmasq`, then reload the main profile with
`sudo apparmor_parser -r /etc/apparmor.d/usr.sbin.dnsmasq`. This is a host
administrator action; Symphony reports the blocker and does not weaken security
policy or retry the mutating Compose command automatically.

During an orchestrated Jira run, Symphony verifies only repositories named by the
exact trusted PlanSpec. When runtime verification runs, it writes a run-specific
`<run-id>-verification.json` manifest under `<workspace>/.symphony/runtime/` with
the PlanSpec binding, affected repositories, hook outcome, runtime check statuses,
sanitized command arguments, and log paths. An enabled runtime is required by
default; `runtime.required: true` makes `test_failed` block in verification and
`environment_blocked` block in verification-environment. Set it to `false` only
when runtime verification should remain advisory.

With `runtime.shutdown_after_handoff: true`, Symphony gracefully stops the
PlanSpec-selected services and their configured/transitive Compose dependency
closure only after the run has completed and Jira handoff has succeeded. Blocked or
failed runs retain their services for diagnosis and manual verification. Shutdown
uses Compose `stop` with the configured `shutdown_grace_seconds`; it does not run
`down`, remove containers, delete volumes, or change the runtime `.env` file. This
preserves shared databases, networks, and other runtime state while releasing the
run's service closure. Operators can invoke the same bounded teardown explicitly
with the `runtime ... shutdown` command shown above.

## Commands

```bash
python3 -m symphony_jira validate ./WORKFLOW.md
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100 --dry-run
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100
python3 -m symphony_jira run ./WORKFLOW.md
python3 -m symphony_jira dashboard ./WORKFLOW.md --port 3333
python3 -m symphony_jira runtime ./WORKFLOW.md verify --workspace /path/to/workspace --repository cpm
python3 -m symphony_jira runtime ./WORKFLOW.md shutdown --workspace /path/to/workspace --repository cpm
```
