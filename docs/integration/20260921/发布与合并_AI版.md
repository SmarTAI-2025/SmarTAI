# SmarTAI publication continuation contract — 2026-09-21

## Live publication supersedes historical pending statuses

Repository `SmarTAI-2025/SmarTAI`; PR #79; URL https://github.com/SmarTAI-2025/SmarTAI/pull/79 ; head branch `fix/verified-integration-20260920`; base `main@edfdabceca369ccba3f1d62178908303b8f8dec4`. Device authorization succeeded as SmarTAI-2025. The code branch has been pushed and exact remote SHA read back. The PR is open, not draft and not merged. No approval/merge/ruleset modification was performed by the agent.

Runtime SHA `9f762640a9cd7bd24016cea0cb0d52e07a808a7b`. Later delivery-only commits must have no diff under backend/, frontend/, render-requirements.txt, security-constraints.txt or runtime configuration before this evidence may be reused. Verify actual remote HEAD and latest checks; do not treat a mergeability snapshot as a guarantee after main changes.

## New re-audit repairs since 20260920

`75805ab859614e66bb6bb3c2d3eb1fc29ff4d453`: early return when either normalized extraction stem is empty, preserving invalid candidates for downstream validation instead of uninitialized containment access. Eight failing-before regressions now pass.

`9f762640a9cd7bd24016cea0cb0d52e07a808a7b`: protect differing explicit numeric/dotted major labels before suffix overlap/containment dedup; support Question/Problem/Q/第 plus label punctuation. Twelve cases cover identical/contained/overlap content and four numbering forms. Keep contextual subpart and unnumbered fragment compatibility; do not restore text-only deletion across explicit IDs.

Only `backend/tools/problem_dedup.py` and `backend/tests/test_problem_dedup.py` changed in these two repairs. Original 20260920 inventory retains auth, structure/rubric, reporter, PDF, retention, host-execution denial and dependency repairs. Read its AI summary and accepted interim contracts for implementation details; historical publication status is superseded by this receipt.

## Verification scopes

Full backend at final runtime SHA: 1480 passed,30 skipped,0 failed. Related dedup/structure:64 passed. Frontend unchanged by these Python patches: clean npm ci,378 tests in80 files,typecheck/scope/build/audit passed. PostgreSQL23 tests and migration roundtrip passed; the final pure dedup patch does not alter database code. Final runtime SHA E2E was rerun during publication:2 passed. The fake-provider/local-browser scope is not live SMTP/models/AWS/S3/Windows/formal OCI acceptance.

Actual merge-tree result at runtime SHA: `1d7ed9f9e3e2ba26ea6d7ad39d255e2815d7c1b5`; equal to runtime HEAD tree. main is ancestor. Original PR head snapshots were unchanged before close. #59–#67 heads are ancestors; #40/#58 intentionally not included. #60 unrelated document deletions are excluded from the integrated tree while its commit ancestry remains.

## Authorized old-PR closure

All #40,#58,#59,#60,#61,#62,#63,#64,#65,#66,#67 were commented and closed only after #79 and its remote ref existed and matched. Per-PR comments state scope, reviewer focus, dependency consolidation and why not to merge independently. No branch deletion. Closed/superseded is not merged. Exact URLs/IDs/head snapshots are in publication-receipt.json. Never reopen/replay those PRs onto #79 simply because they are not marked merged.

## Review and merge contract

One qualified non-PR-author formal approval is required; stale approvals dismissed after push; actual review threads must be resolved. No named required reviewer/CODEOWNER. lyj00422 and Annie-Geng have write permission; #79 author is SmarTAI-2025. Recommend one competent auth/storage/migration reviewer, not mandatory approval from every downstream owner. No self-approval, simulated approval or rule bypass. The owner manually selects Create a merge commit for #79 into main. No retarget/rebase of old stacked PRs is needed.

ljd05/06: after merge and before related wiring, consume current major IDs, PDF worker, artifacts and retained/task_only storage; do not duplicate recognition/workers. dsy07/09: adapt subpart blocks into one major answer without silently overwriting fragments; keep confirmed mapping, review state and not-executed semantics. xts08: provide canonical runner contract/adapter and preserve production host-execution denial until formal safe cutover. gxr11 reads actual candidate UI;12/13 consume migration0016 and auth/storage contracts but remain separate unimplemented work. These interface reads are not additional pre-merge signoffs.

## Remote checks and receipts

A real GitHub Actions run started successfully after publication; the historical billing-blocked state is not reused. Later docs pushes create a new HEAD/check run. The final verification comment must name the actual final HEAD and distinguish local evidence, remote CI, spike isolation and unverified deployment. Capture final checks/jobs/logs separately; do not fabricate a CI pass in this pre-final-check snapshot.

Publication does not authorize production deployment/real users. Live email/model/provider quality, actual AWS/S3, Windows, full formal OCI/D3 and public-admin isolation remain outside this PR. Zero scanner findings and passing tests do not prove absence of all defects.

## Workspace and credential boundary

Work remains under /tmp/smartai-integration-20260920-sMxfeA plus the explicitly permitted Conda interpreter. New login material is stored outside the repository under the dedicated followup/gh directory. Never print, commit or include that directory in an archive. Keep original branches and fresh git bundle. Do not execute the historical publish_candidate.py unchanged: its frozen runtime SHA3977108 is stale; use verified current state, not a bypass of its checks.
