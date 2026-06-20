---
name: "keep-codex-fast"
description: "Use when Codex feels slow or bloated, when local sessions/logs/worktrees/config have grown over time, or when a user wants safe maintenance for Codex Desktop/CLI state. Provides a read-only report by default, backs up before applying changes, archives instead of deleting, normalizes Windows extended paths, prunes dead config projects, rotates large logs, and moves stale worktrees."
metadata:
  short-description: "Safe Codex local-state maintenance"
---

# Keep Codex Fast

Use this skill to inspect and safely maintain local Codex state. The goal is to reduce local drag without surprising the user or losing continuity.

Primary principle: preserve continuity before applying changes. For active repo chats the user may continue, recommend a comprehensive handoff document and reactivation prompt before archiving anything.

## Safety Rules

- Inspect before mutating.
- The first run must be report-only. Report mode must not write files, create backups, move folders, or change local Codex state.
- Back up before applying changes. Use `--backup-only` when the user wants backups without moving or changing local state.
- Archive or move files instead of deleting them. Do not permanently delete user chats, logs, worktrees, memories, skills, plugins, or automations.
- Write manifests and restore scripts when sessions/worktrees are moved.
- If Codex is running, default to report-only. Apply broad maintenance only after Codex is closed or when the user explicitly accepts waiting for Codex to exit. The narrow `--apply --hot-normalize-paths` mode may run while Codex is open because it backs up first and only aligns SQLite path fields to Codex Desktop's active `\\?\` path convention.
- Never modify or copy credential files unless the user explicitly asks for that. Back up memory/skill/plugin/automation files before touching local state.
- Treat backup folders as private local artifacts because they can contain Codex metadata. Do not ask users to publish or share backups unless they have reviewed them first.
- Do not print raw thread IDs, chat titles, local paths, or process paths unless the user asks for details or runs `--details`.
- Before applying changes, tell the user to create handoff docs for active repo chats they may continue.
- Before archiving any active repo chat the user may want to continue, recommend creating a comprehensive handoff doc plus a reactivation prompt.
- Do not archive old-but-important active repo chats until the user either confirms a handoff exists or confirms they do not need one.

## Mental Model

There are three modes:

- Inspect: report-only, no writes.
- Maintain: normal `--apply`; backs up, archives old sessions, moves stale worktrees, rotates logs, prunes dead config, and normalizes paths. It does not trim thread title/preview metadata.
- Hot path repair: `--apply --hot-normalize-paths`; backs up and aligns SQLite path fields to Codex Desktop's active `\\?\` path convention even while Codex is running. It does not archive sessions, rotate logs, move worktrees, prune config, or repair title/preview metadata.
- Optional repair: `--apply --repair-thread-metadata-bloat`; shortens oversized SQLite display title/preview metadata after backup. The rollout transcript stays intact.
- Optional malformed-task archive: `--apply --archive-malformed-local-tasks`; archives active no-user-event local task sessions with suspicious workspace roots such as `/` or OS temp folders after backup.
- Targeted thread recovery: `--apply --recover-thread-id THREAD_ID`; backs up SQLite and matching thread automations, refreshes one thread's archived state, restores its original final active/archived state, restores missing matching automation definitions, and skips broad cleanup.
- Detected thread recovery: `--apply --recover-detected-threads`; backs up SQLite and matching thread automations, refreshes recent broken-thread candidates found in `logs_2.sqlite`, restores missing matching automation definitions, and skips broad cleanup.

## Default Workflow

1. Reassure the user: the first run is read-only, privacy-safe, and the skill archives instead of deleting when changes are later applied.
2. Run the bundled script in report mode:

```bash
python scripts/keep_codex_fast.py
```

3. Summarize:
   - active session size
   - archived session size
   - largest active sessions
   - thread metadata bloat: active title/preview character totals, max title/preview lengths, and over-limit counts
   - malformed local task candidates: no-user-event active local tasks with suspicious workspace roots
   - stale worktree candidates
   - log size
   - bad Windows `\\?\` path counts
   - config project prune candidates
   - top Node/dev processes
4. Before applying changes, recommend that the user create handoffs for all active repo chats they may continue. Explain that handoffs let them archive heavy chats and resume from docs in fresh threads.
5. Identify large/old active repo chats that may still matter. For each one the user wants to continue, create or update:
   - a repo-local handoff doc
   - a reactivation prompt that can start a fresh chat without losing the thread
6. If the user wants to apply the recommended maintenance, ask them to close Codex or use `--wait-for-codex-exit`, then run:

```bash
python scripts/keep_codex_fast.py --apply --archive-older-than-days 10 --worktree-older-than-days 7
```

If the user has just created a handoff inside an old chat, explain that `--archive-older-than-days` uses `updated_at` by default. Use `--archive-age-field created_at` when the user's intent is "archive chats created before the threshold even if a recent handoff updated them":

```bash
python scripts/keep_codex_fast.py --apply --archive-older-than-days 10 --archive-age-field created_at
```

If the user wants to archive exactly one confirmed session, use a targeted archive instead of a broad `--archive-older-than-days 0` sweep:

```bash
python scripts/keep_codex_fast.py --apply --archive-thread-id THREAD_ID
```

or:

```bash
python scripts/keep_codex_fast.py --apply --archive-rollout-path /path/to/rollout.jsonl
```

If the current issue is only stale resume/automation path drift caused by Codex Desktop's `\\?\C:\...` active path convention, it is acceptable to hot-repair just active SQLite path fields while Codex is running:

```bash
python scripts/keep_codex_fast.py --apply --hot-normalize-paths
```

Do not combine this with archival or metadata-bloat expectations. Hot mode intentionally suppresses archiving, log rotation, worktree moves, config pruning, config path rewrites, and title/preview repair while Codex is open.

If Codex immediately writes the extended paths back from in-memory state, use a bounded watch window:

```bash
python scripts/keep_codex_fast.py --apply --hot-normalize-paths --hot-normalize-watch-seconds 300 --hot-normalize-interval-seconds 30
```

If the user wants to recover a Codex Desktop thread that shows `Error submitting message`, `Error creating task`, or `agent loop died unexpectedly`, use `$codex-thread-recovery` when available. If they specifically want the `keep-codex-fast` storage-level tool path, run the targeted recovery mode:

```bash
python scripts/keep_codex_fast.py --apply --recover-thread-id THREAD_ID
```

That mode is backup-first and intentionally narrow. It refreshes only the requested row's archive state, snapshots automations that target that thread, restores missing matching automation definitions, and then exits without stale-session archiving, path cleanup, config pruning, log rotation, malformed-task archiving, or metadata repair.

Normal report mode scans recent `logs_2.sqlite` entries for agent-loop/start-turn failure signatures and prints `broken_thread_candidates`. Use `--details` to show raw ids:

```bash
python scripts/keep_codex_fast.py --details --broken-thread-lookback-hours 72
```

If those candidates should be recovered through the local storage path, use:

```bash
python scripts/keep_codex_fast.py --apply --recover-detected-threads
```

This is still explicit and backup-first. It should not run silently as broad scheduled maintenance.

7. Verify after applying:

```bash
python scripts/keep_codex_fast.py
```

8. Ask whether the user wants a recurring report-only reminder:
   - weekly for heavy Codex use across many repos/terminals
   - biweekly for lighter use
   - no reminder if they prefer manual maintenance

If the user wants automation and the Codex app automation tool is available, create only a recurring report/reminder automation. Do not recommend recurring mutating maintenance, because automation cannot know whether the user created handoffs. The prompt must say not to pass `--apply`, not to archive/move/prune/rotate/normalize/delete/mutate local state, and to remind the user that manual apply should happen only after handoffs are confirmed and Codex is closed.

## What Apply Does

- Backs up important metadata to `~/Documents/Codex/codex-backups/keep-codex-fast-*`.
- Writes restore scripts and copy-paste-safe Python restore commands for moved sessions/worktrees and repaired thread metadata.
- Archives old non-pinned sessions to `~/.codex/archived_sessions/`.
- Uses `updated_at` for age-based session archiving by default, or `created_at` with `--archive-age-field created_at`.
- Supports targeted session archiving with `--archive-thread-id` or `--archive-rollout-path`, still backup-first and archive-only.
- Supports targeted thread recovery with `--recover-thread-id`, still backup-first and one-thread only, including matching automation backup/restore.
- Reports recent broken-thread candidates from `logs_2.sqlite`; with `--recover-detected-threads`, refreshes only detected rows that still exist in local state and preserves matching automations.
- Normalizes Windows extended paths like `\\?\C:\...` inside local SQLite text fields and selected metadata files such as `config.toml`.
- With `--apply --hot-normalize-paths`, aligns active SQLite path fields to the active `\\?\` convention while Codex is running, after backup.
- With `--hot-normalize-watch-seconds`, repeats only that hot path alignment for a bounded window so automations/resumes can get past a running app that rewrites active rows.
- Prunes missing/temp project blocks from `config.toml` and writes UTF-8 without BOM.
- Moves stale worktrees to `~/.codex/archived_worktrees/` and writes a restore helper.
- Rotates `logs_2.sqlite*` and `log/codex-tui.log` into `~/.codex/archived_logs/` only when above the threshold.
- Reports heavy Node processes without killing them.
- Reports pathological active thread titles and `first_user_message` previews. It only repairs them when the user explicitly opts in with `--repair-thread-metadata-bloat`.
- Reports malformed active local task sessions with `has_user_event=0` and suspicious `cwd` values. It only archives them when the user explicitly opts in with `--archive-malformed-local-tasks`.

Report mode does none of those mutations. It only prints counts and pseudonymous candidates. Use `--details` when raw IDs, titles, or paths are needed for diagnosis.

## Recommended Policy

- Keep only the last 7-10 days of non-pinned chats active.
- Use handoff docs for important old threads.
- Start fresh threads from handoff docs instead of repeatedly resuming giant chats.
- Run weekly maintenance if Codex is used daily across many repos/terminals.
- Offer weekly or biweekly report-only reminders after the first successful apply; do not assume the user wants recurring maintenance.
- When in doubt, leave a chat active or ask the user. Never archive a chat that is pinned, current, or explicitly marked as still needed without a handoff.
- Treat title/preview repair as metadata repair only. The full rollout transcript remains in the session JSONL; bounded SQLite fields are for list/navigation display.
- Treat malformed local task archiving as cleanup for synthetic/no-user-event sessions. It should not target normal chats with user events.
- Treat hot path repair as path alignment only. It should not archive, prune, rotate, move, or rewrite config while Codex is running.
- Treat targeted thread recovery as a liveness nudge, not broad maintenance. Prefer Codex app thread APIs when available; use the script when the user wants the local tooling path.

## Thread Metadata Bloat

Codex Desktop can become slow when `threads.title` or `threads.first_user_message` stores a full prompt/history-sized value instead of a display title or preview. This affects the thread list/navigation path before the UI renders anything.

The script reports active thread count, total title/preview characters, maximum title/preview length, active titles over the configured title limit, and active previews over the configured preview limit and over 10k characters.

Normal apply mode reports metadata-bloat candidates but does not repair them. If the user explicitly opts in, after backups and only when Codex is not running, run:

```bash
python scripts/keep_codex_fast.py --apply --repair-thread-metadata-bloat
```

That bounds active `threads.title` and `threads.first_user_message` values. Defaults are 120 characters for titles and 240 characters for previews. If a thread already has a friendly name in `session_index.jsonl`, the repair writes that name back into the SQLite display title instead of replacing it with a shortened prompt, including already-bounded prompt fallback titles from earlier repairs.

The targeted repair manifest stores the old full title/preview values so the change can be reversed. Treat `thread-metadata-repairs.jsonl`, `restore-thread-metadata.py`, and the whole backup folder as private local artifacts.

This is a local maintenance workaround for metadata bloat. It does not solve app renderer hydration of very large rollout histories; that needs upstream staged/paged thread loading.

## Malformed Local Task Sessions

Codex Desktop can become slow when active local task rows have no user event and an unusable workspace root, such as `/` or an OS temp folder. Third-party app-server integrations can create this state. The visible symptom is repeated `No cwd found for local task` log lines for those conversation IDs while Desktop thread-list rendering becomes sluggish.

The script reports these candidates in report mode and normal apply mode. Normal apply does not archive them. If the user explicitly opts in, after backups and only when Codex is not running, run:

```bash
python scripts/keep_codex_fast.py --apply --archive-malformed-local-tasks
```

That moves matching rollout JSONL files into `~/.codex/archived_sessions/`, marks those rows archived in SQLite, and writes a restore manifest/script. The predicate requires `has_user_event=0`, an active/unarchived thread, a suspicious `cwd`, and a rollout file under `~/.codex/sessions`.

## Handoff Doc + Reactivation Prompt

For important active repo chats, create a handoff before archiving. Prefer a repo-local path such as `docs/codex-handoffs/YYYY-MM-DD-topic.md` or a user-approved docs location.

Use `references/handoff-template.md` when the user wants a concrete template.

A handoff document converts an old chat into durable project memory. It should let a fresh Codex thread continue after reading the repo and the handoff, without needing the original chat history.

Offer this prompt for each active repo chat the user may want to continue:

```text
Create a comprehensive handoff document for this repo/session before I archive Codex history.

Include:
- repo/path and branch
- current goal
- what we already completed
- files touched or investigated
- commands/tests already run
- known errors, warnings, or failing checks
- open decisions
- constraints, user preferences, and do-not-touch areas
- the next 3-7 concrete steps

Also include a reactivation prompt I can paste into a fresh Codex chat so it can continue from this handoff without relying on the old chat context.

Save the handoff in a sensible repo-local place like docs/codex-handoffs/YYYY-MM-DD-topic.md unless this repo already has a better handoff location.
```

The handoff should capture:

- repo/path and branch
- current goal
- what was already done
- key files touched or investigated
- commands/tests already run
- known failures or warnings
- open decisions
- next 3-7 concrete steps
- any constraints, user preferences, or "do not touch" areas

Add a reactivation prompt at the top or bottom:

```text
We are continuing from this handoff. Read this document first, inspect the current repo state, verify what still applies, and continue from the next steps without assuming the old chat context is available.
```

## Automation Reminder Prompt

Offer this after the first report/apply/verify cycle:

```text
Use $keep-codex-fast to create a recurring Codex maintenance reminder.

Schedule it weekly if I use Codex heavily, or biweekly if that seems safer.

The reminder should:
- run the keep-codex-fast report first
- never pass --apply or run mutating maintenance automatically
- never archive, move, prune, rotate, normalize, delete, or mutate local Codex state
- remind me to create comprehensive handoff docs and reactivation prompts for active repo chats before any manual apply
- summarize active session size, archived session size, extended path candidates, old session candidates, worktree candidates, log size, and top Node/dev processes
- summarize malformed local task candidates without archiving them
- report heavy Node/dev processes without killing them
- tell me that manual apply should only happen after I confirm handoffs exist or are not needed and Codex is closed
```

## Anti-Patterns

Avoid these behaviors:

- deleting sessions, logs, worktrees, memories, plugins, or skills permanently
- applying changes while Codex is actively writing the DB
- archiving important repo chats before creating handoff docs
- treating active history size as "bad" without checking whether the user needs continuity
- treating preview metadata repair as deletion of the actual rollout transcript
- killing Node/dev processes automatically
- rewriting `config.toml` without a backup and parse check
- writing UTF-8 TOML with a BOM on Windows
- promising speed gains as universal fact; frame improvements as local-state maintenance results
- making users feel like they did something wrong by using Codex heavily

## User-Facing Caution

Tell users this does not permanently delete chats, worktrees, or logs. It moves them into archive folders and writes restore helpers. The only removed content is stale metadata, such as project entries pointing to folders that no longer exist, and even that happens after backing up `config.toml`.

Also tell users that thread title/preview bloat repair is not part of normal apply. Normal use only reports title/preview bloat. Recommend `--repair-thread-metadata-bloat` only as an optional extra when the report shows large metadata payloads and the user understands that SQLite display metadata will be shortened while the real transcript remains in rollout JSONL.

Also tell users backup folders can contain private local Codex metadata, including old thread titles and first-message previews. They should keep backups local and avoid publishing or sharing them unless they have reviewed what is inside.
