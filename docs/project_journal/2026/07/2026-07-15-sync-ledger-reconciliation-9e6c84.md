---
id: 20260715-9e6c84
title: Sync Ledger Reconciliation
status: completed
created: 2026-07-15
updated: 2026-07-17
branch: codex/sync-ledger-reconciliation
pr:
supersedes: []
superseded_by:
---

# Sync Ledger Reconciliation

## Summary
- The personal sync installer now reconciles public and private manifests through validated ownership state and append-only removal metadata.

## Current State
- Exact legacy links can be quarantined from `removed_links`; files, directories, unrelated links, and symlink parents are preserved.
- CI validates removal history against the unique complete Release that is a Git descendant of every other complete Release candidate; release SHAs come from the unique uploaded archive/checksum pair, are batch-bound through local tags, and do not depend on a potentially movable `target_commitish` branch. Explicit target SHAs must still match, publish timestamps do not define version order, and incomparable histories fail closed.
- Release-manifest validation unions the normalized `removed_links` history from every unique complete Release before checking the baseline transition. Historical entries must remain exact across releases and the current manifest, while a later commit may precisely restore a non-legacy tombstone that an intervening release dropped; historical replacement-retirement obligations remain enforced.
- Portable target identities reject case and Unicode aliases; replacements are verified before obsolete links are removed.
- Cross-version target checks include both installed and incoming manifests, and replacement-retirement cycles are rejected.
- Install and rollback activation plus overlay uninstall now use a durable exact-inode write-ahead log: a fixed regular-file pointer at the stable sync-home root binds one fsynced quarantine batch containing staged link inodes, preimages, and exact managed-state before/after evidence.
- Each WAL batch records exact before/after evidence for every `current` pointer and managed link in the corresponding ledger states, including unchanged paths: present entries use hard-linked inode claims, while state-claimed create repairs use parent-bound absence records. Bounded metadata parsing validates and indexes records and claims in `O(n)` time.
- Install, GitHub download, rollback selection, and uninstall preflight validate pending WAL state before source or network access; dry runs remain read-only, while mutating paths recheck beneath the installation lock before recovery or new planning.
- A `current`/ledger SHA mismatch never authorizes adoption of an unledgered link; only exact WAL evidence, or explicit first-bootstrap history, may establish ownership.
- An existing-ledger SHA mismatch or a state-claimed path replaced by a foreign node fails closed before pointer publication. A state-claimed missing symlink remains repairable through exact absence evidence and no-replace creation.
- Ledger-claimed links that are already missing can be retired through a parent-bound `retire-absent` WAL record; parent binding is refreshed after sibling creates, and committed recovery preserves files that appear only after the state commit.
- First-bootstrap overlay uninstall keeps the absent canonical ledger as the transaction before-state while using reconstructed ownership only for planning, so both precommit rollback and postcommit recovery remain exact.
- WAL v4 records the canonical directory inode and full-tree digest for every release named by the before/after ledgers; phase validation refuses pointer cleanup when a release tree is edited or replaced, even by a same-content directory.
- Managed-state publication remains precommit until an inode-bound, fsynced commit marker is published after all release, link, and state checks. Recovery rolls back an unmarked exact after-state, while a marked transaction never infers rollback from a missing canonical ledger.
- An uncommitted first-bootstrap overlay uninstall that restores an absent managed-state file re-enters legacy ownership planning from the exact recovered before-state, so a retry cannot remove only the overlay `current` pointer while stranding its links.
- Before a recovered precommit pointer is cleared, every planned create revalidates its descriptor-bound before-state absence. A foreign node keeps the pointer and evidence in place and cannot be adopted by the subsequent legacy bootstrap.
- State publication uses exclusive creation and descriptor-bound verification; concurrent or unexpected content is preserved in quarantine instead of overwritten.
- State loads, transaction snapshots, and match checks bind parent and file descriptors, reject non-regular or oversized ledgers, and verify identities around bounded reads.
- Release staging copies from descriptor-bound sources, validates the complete staged manifest, sanitizes modes, and retains raced objects without publishing an unverified SHA.
- Expected full-tree identity and the canonical release-directory inode remain bound across activation and ledger publication; any drift rolls back state, links, and `current` while leaving raced content in place.
- Same-SHA releases compare coherent descriptor-bound tree snapshots, require actual installed modes to be sanitized, and use bounded non-blocking source reads before reuse and after publication.
- Strict Git snapshots and archive extraction reject exact duplicates plus case, Unicode-normalization, implicit-directory, and file/directory path aliases before writing a package tree.
- Private base release selection is parsed from the same validated manifest payload instead of reopening a mutable path.
- Removal history rejects unknown fields, and active cross-owner replacements remain required until their tombstones are explicitly retired.
- Desired-link, replacement, refresh, and published-ledger checks bind parent directories and roll back when mandatory links disappear or drift.
- Release SHAs are validated at every install boundary, and beneath-root helpers reject dot-component and absolute/relative traversal attempts.
- Overlay uninstall uses historical removals for cleanup while deriving replacement obligations only from the next active manifest set.
- Archive extraction creates and publishes every member relative to bound directory descriptors, never overwrites concurrent leaves, and validates the complete extracted tree before returning its release root. It retains directory identities instead of one descriptor per directory, then reopens each path component with `O_NOFOLLOW` and verifies the recorded inode, keeping descriptor use constant even for large directory sets.
- Checksum verification and extraction share one immutable unlinked archive snapshot; bounded regular-file reads, member and expanded-size limits, PAX metadata accounting, and complete content identities close path-replacement and in-place rewrite races.
- Truncated gzip streams and corrupt deflate payloads are normalized to sync-domain errors during both archive inspection and extraction, avoiding raw decompressor exceptions after checksum acceptance.
- Source release expectations flow from verified download through staging, while every active owner remains descriptor-bound across activation and managed-state publication; unchanged-owner or uninstall drift triggers rollback without deleting raced trees.
- Replacement dependencies include `replace` and `quarantine-replace`, use stable topological ordering, and explicitly validate same-path migrations after the new link is created.
- Safe release-root container modes no longer change content identity, while new archives publish an explicit deterministic `0755` package root and strict verification still rejects unsanitized modes.
- Archive member paths have bounded UTF-8 length, component length, and depth; embedded NUL paths are rejected before extraction, portable path conflicts use a bounded component trie, and the 10,000-entry ceiling counts every explicit member plus every implicit trie path before an extraction destination is created.
- The exact ledger snapshot loaded for planning remains the transaction baseline, and established ledgers only retain targets already owned or created/replaced by the current transaction.
- Ledger planning now binds the canonical state parent and file inode, while rollback quarantines only the exact inode published by the transaction and preserves same-content racers.
- The install lock keeps descriptor-bound parent and lock files for the whole critical section and revalidates both canonical identities after acquisition and immediately before the transaction begins.
- The sync-home root itself is opened from a bound parent descriptor with `O_NOFOLLOW`; lock, pending-pointer, reconciliation, and internal-directory operations reject preexisting or concurrent home-root symlink replacement.
- Published state, managed-link parent/inode/target snapshots, owner-to-release bindings, `current`, and canonical release directories are revalidated after full release scans; concurrent same-target replacements are preserved rather than reclaimed during rollback.
- Strict package Git inventories enforce streaming stdout/stderr limits with terminate/kill/reap cleanup, committed manifests are size-checked before bounded reads, and redundant live worktree traversal is removed from strict snapshots.
- Strict package inventory filtering uses component-prefix tries instead of comparing every Git entry with every manifest source or copying every possible path prefix. Git pathspecs collapse to unique top-level roots under a 32 KiB argument budget and fall back to bounded full inventories when necessary; directory descendants are assigned during one tree scan, so duplicate, nested, exact-file, and deeply nested sources remain bounded by inventory size and path depth without approaching `ARG_MAX`.
- Strict package creation validates complete root-prefixed member paths, count, depth, per-blob size, expanded tar size, and compressed output before publication; actual blob reads are bounded by the preflighted size, duplicate raw Git paths fail closed, and archive/checksum failures remove partial outputs.
- Runtime parsing plus strict and non-strict package creation cap active manifests at 9,999 links so the WAL retains capacity for a `current` action; both package paths share the same member, path, portable-collision, expanded-size, and tar-stream preflight before creating output.
- Install preflight validates aggregate action, record, claim, release-expectation, managed-state, and conservatively projected v4 WAL metadata capacity before release staging. Every incoming owner reserves a worst-case `current` record, batch names are byte-bounded, and the real metadata serializer retains the same streaming byte limit as defense in depth.
- Manifest-change validation projects each complete single-owner transition through the runtime-owned serializer model: healthy claims, missing-link creates, replacements, removals, `retire-absent`, quarantine actions from all declared removal history, release expectations, managed state, and the `current` switch must fit both the 10,000-record/claim limits and the exact 16 MiB WAL byte limit before publication. The current profile is cached across release-history comparisons without rebuilding a 16 MiB payload.
- Committed WAL batches are reclaimed through inode-bound external cleanup tickets published before the active pointer is cleared. Cleanup recursively stays beneath the exact quarantine root without following links, deletes the ticket last, resumes after interruption, and uses a bounded rotating startup scan so an old failing ticket cannot starve later work.
- Cleanup ticket and temporary-file deletion first moves the named object to a high-entropy retained name, verifies the moved inode, mode, and payload through an open descriptor, and deletes only that exact object. Concurrent replacements remain preserved, while batch-root cleanup uses a deterministic isolation name so interruption between rename and `rmdir` resumes safely.
- Each batch-content cleanup attempt, including interrupted recovery, moves every supported entry to a fresh parent/inode-encoded active name before identity revalidation and deletion. Mismatches become durable retained blockers that survive retries and require operator cleanup.
- The cleanup race model covers non-adversarial concurrent path changes around private mode-`0700` sync state. A malicious process running as the same uid is outside scope because it already has equivalent authority to mutate the ledger, release trees, and quarantine directly; portable macOS/Linux APIs do not provide inode-conditional unlink.
- At most eight generated quarantine transaction batches may be retained, counting both canonical and cleanup-isolated batch names. Pointerless staging failures and deliberately preserved audit batches therefore remain untouched but cannot grow without bound; new transactions fail before allocating another batch until cleanup or operator intervention restores capacity.
- Active, removed, and replacement targets are rejected before portable-key construction when they exceed 4,096 UTF-8 bytes, 64 components, or 255 UTF-8 bytes in any single component. Target overlap and cross-version hierarchy checks compute each portable identity once and use sorted adjacent comparisons, bounding validation to `O(n log n)`.
- Runtime, builder, and manifest-change validation reject invalid UTF-8 scalars throughout manifest JSON plus NUL or invalid encoding in path fields, and builders enforce the 4 MiB limit on the final serialized release manifest.
- GitHub release assets are selected by validated REST asset ID and advertised size, then streamed through bounded `gh api` stdout into no-overwrite partial files. Publication keeps the verified partial descriptor open, links within a bound destination directory, and checks that the published inode still matches before accepting it. Cleanup isolates named entries under unpredictable no-replace names and deletes only the inode bound to that descriptor, preserving concurrent replacements. Overflow terminates and reaps the child process, and manifest parse/canonicalization failures are normalized to domain errors.
- Reconciliation plans bind the nearest existing ancestor plus the exact parent and leaf inode/target; missing parents are published exclusively and reused only through transaction-owned identities.
- Install, uninstall, and `current` mutations fail closed on same-target inode or parent replacement, while failed destructive transactions restore the original quarantined inode without overwriting concurrent content.
- Manifest-change validation applies the same 4 MiB raw and formatted payload limits to current and historical manifests, resolving exact Git commits and blobs before bounded reads.
- Linux and macOS CI both exercise the platform-specific no-replace reconciliation path.
- Strict builders and manifest validators disable Git replacement objects for every commit, tree, blob, ancestry, and diff read, so package and baseline identities cannot be redirected through `refs/replace`.
- Release-manifest serialization rejects oversized string tokens before encoder materialization and streams formatted JSON into a bounded buffer; deep or oversized programmatic payloads fail with domain errors.
- Current manifest reads validate a safe relative path before repository access, then bind the repository root, every ancestor, and the regular-file leaf through non-blocking no-follow descriptors across the bounded read.
- Portable target overlap checks precompute normalized keys and use adjacent prefix comparisons, reducing validation from quadratic to `O(n log n)` without weakening case or Unicode collision checks.
- Runtime, builder, and manifest-change validation now share owner, override, reserved-target, source-kind, `base_release`, removed-link field, and retirement-graph semantics, including exact `OWNER/REPOSITORY` validation and fail-closed unknown-field rejection for base releases; current validation revalidates the manifest plus every observed source/ancestor as one live worktree snapshot, while the baseline remains commit-bound.
- Public package and manifest validation now reject active removal records whose replacement target is neither present nor explicitly retired, matching the runtime release-set obligation before publication.
- GitHub release-history validation bounds each response, the page count, the total release count, and batch Git input/output; it normalizes malformed or over-deep JSON without traceback leakage and resolves commit-graph order with a fixed number of Git processes. Every authenticated complete Release manifest is deduplicated and batch-loaded to prove skip-upgrade target hierarchy and WAL capacity, while all declared historical removal targets remain subject to the same checks for legacy and not-yet-released state.
- GitHub CLI JSON parsing normalizes oversized integers and recursion failures for both single and concatenated-page responses, so malformed remote data cannot escape the synchronizer error boundary with a traceback.
- Status reconciliation reads ledger-recorded links through descriptor-bound snapshots, rejecting managed-link parent replacement instead of following a redirected path.
- Cooperative installers lock the stable sync-home directory inode for the entire transaction, so replacing `personal-sync` or its named lock cannot admit a second synchronizer.
- Current-release reads bind the owner root, `current`, `releases`, and the selected release through no-follow directory descriptors, then recheck every name and parent binding before returning a SHA; replacing the checked owner root can no longer redirect the read to a coherent attacker-controlled tree.
- Same-content link and ledger inode racers moved during quarantine are restored exactly with no-replace renames; an occupied original name is preserved and reported instead of overwritten.
- Overlay uninstall applies managed-link changes first, removes the outgoing `current` pointer second, and publishes the owner-free managed state last; precommit rollback and recovery reverse those mutations.
- Overlay uninstall keeps the outgoing release descriptor-bound through state publication and rollback, retaining pending evidence whenever release identity drift makes rollback unsafe. An already-missing outgoing `current` pointer is represented by a durable `current/retire-absent` record, including bounded metadata, exact before-state transition validation, claim omission, and foreign-target-preserving recovery.
- Combined manifest validation rejects portable-key strict ancestor conflicts between active targets and historical `removed.target` entries for every owner, while continuing to allow exact migrations, removed-vs-removed history, and replacement-target-only hierarchy.
- Runtime, package, and manifest-change validation cap owner path components at 255 UTF-8 bytes, including owners embedded in replacement-retirement keys.
- Public release verification now passes the exact checksum-verified archive snapshot directly into extraction in both publication jobs.
- Planned installs, public upgrades, and public rollbacks honor every retained overlay's optional `base_release.sha` before reconciliation or release staging. Pinned overlays must match the selected public SHA, paired installs remain supported, and unpinned overlays retain their existing follow-selected-base behavior.
- Runtime and validation helpers retain Python 3.9 compatibility, with a dedicated CI lane covering the previously incompatible pending-state and release-history paths.
- Manifest and durable synchronizer state versions require exact JSON integers, so booleans and numerically equal floats cannot select a schema version.
- Checksum verification and archive extraction share one immutable snapshot, preventing a verified archive path from being replaced before extraction.
- Overlay verification accepts exact desired symlinks that intentionally remain outside the ownership ledger, while still rejecting conflicting recorded ownership and preserving those links on uninstall.

## Next Steps
- Monitor the first packaged release that exercises managed reconciliation and the dependent private overlay.
- Add a combined public/private manifest capacity gate when the private release job has both exact manifests; the installer already performs this aggregate preflight and fails safely, while repository CI currently proves capacity one owner at a time.

## Evidence
- Repository test command — 604 tests passed with Python 3.13.0 after integrating the latest `origin/master`; the upstream bounded-output consolidation replaced five guideline tests with one aggregate contract test.
- Reconciliation safety module — 268 tests passed as part of the repository suite.
- Runtime module — 153 tests passed.
- Package builder safety module — 57 tests passed as part of the repository suite.
- Manifest change validation module — 77 tests passed as part of the repository suite.
- Release baseline validation module — 25 tests passed.
- Dedicated Python 3.9 compatibility selection — 9 tests passed.
- Changed-file `ruff`, repository-wide `ruff`, `actionlint`, manifest change validation, project journal validation, Python compilation, and `git diff --check` passed.
