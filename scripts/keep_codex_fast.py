#!/usr/bin/env python3
"""Backup-first Codex local-state maintenance.

Default mode is a read-only, privacy-safe report. Use --apply to archive/move/normalize.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
THREAD_REFERENCE_RE = re.compile(
    r"(?:thread|conversation)(?:\s+id)?\s*[:=]?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
PROJECT_HEADER_RE = re.compile(r"^\[projects\.([\"'])(.+)\1\]\s*$")
TEMP_PROJECT_RE = re.compile(
    r"(\\AppData\\Local\\Temp\\|/AppData/Local/Temp/|\\Temp\\codex-|/Temp/codex-|\\Temp\\spark-|/Temp/spark-)",
    re.I,
)
TEMP_LOCAL_TASK_CWD_RE = re.compile(
    r"("
    r"^/(?:private/)?var/folders/.+/T$"
    r"|^/(?:tmp|var/tmp)(?:/|$)"
    r"|^[A-Z]:\\Users\\[^\\]+\\AppData\\Local\\Temp(?:\\|$)"
    r"|^\\\\\?\\[A-Z]:\\Users\\[^\\]+\\AppData\\Local\\Temp(?:\\|$)"
    r")",
    re.I,
)
DEFAULT_TITLE_LIMIT = 120
DEFAULT_PREVIEW_LIMIT = 240
NORMALIZE_TEXT_FILES = [
    "config.toml",
]
PATH_COLUMN_HINTS = ("path", "cwd", "file", "folder", "dir", "root", "source", "workspace")
WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:\\")


@dataclass
class SessionCandidate:
    size: int
    thread_id: str
    title: str
    source: Path
    relative: Path
    created_at: int | None
    updated_at: int | None
    reason: str


@dataclass
class MalformedLocalTaskCandidate:
    size: int
    thread_id: str
    title: str
    cwd: str
    reason: str
    source: Path
    relative: Path
    updated_at: int | None


@dataclass
class ThreadMetadataRepair:
    thread_id: str
    old_title: str
    new_title: str
    old_preview: str
    new_preview: str


@dataclass
class ThreadArchiveRefreshCandidate:
    thread_id: str
    title: str
    was_archived: bool


@dataclass
class BrokenThreadCandidate:
    thread_id: str
    title: str
    failure_count: int
    last_seen: int
    last_activity_at: int | None
    was_archived: bool | None

    @property
    def is_current(self) -> bool:
        return self.last_activity_at is None or self.last_seen >= self.last_activity_at


@dataclass
class ThreadAutomationBackup:
    automation_id: str
    source: Path
    backup: Path


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def codex_home_from_args(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex"


def documents_backup_root() -> Path:
    docs = Path.home() / "Documents" / "Codex" / "codex-backups"
    if docs.parent.exists() or platform.system() == "Windows":
        return docs
    return Path.home() / ".codex" / "backups"


def size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def gb(value: int) -> str:
    return f"{value / 1024 / 1024 / 1024:.3f}"


def mb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f}"


def report(line: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    print(str(line).encode(encoding, errors="backslashreplace").decode(encoding, errors="replace"))


def python_restore_command(script: Path) -> str:
    executable = sys.executable or "python3"
    return f"{shlex.quote(executable)} {shlex.quote(str(script))}"


def sqlite_connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"{canonical_path(path).as_uri()}?mode=ro", uri=True)
    return sqlite3.connect(path)


def canonical_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def codex_processes_running() -> list[str]:
    system = platform.system()
    try:
        if system == "Windows":
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Select-Object Name,ProcessId,CommandLine | ConvertTo-Json -Compress"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            if not output.strip():
                return []
            data = json.loads(output)
            rows = data if isinstance(data, list) else [data]
            hits = []
            for row in rows:
                name = str(row.get("Name") or "")
                cmd = str(row.get("CommandLine") or "")
                pid = row.get("ProcessId")
                if name == "Codex.exe" or (name == "codex.exe" and ("app-server" in cmd or "OpenAI.Codex" in cmd)):
                    hits.append(f"{pid} {name}")
            return hits
        output = subprocess.check_output(["ps", "-axo", "pid=,comm=,args="], text=True)
        hits = []
        for line in output.splitlines():
            lower = line.lower()
            if "codex" in lower and ("app-server" in lower or "openai.codex" in lower or "codex desktop" in lower):
                hits.append(line.strip())
        return hits
    except Exception:
        return []


def wait_for_codex_exit() -> None:
    while codex_processes_running():
        time.sleep(2)


def sqlite_backup(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite_connect(src, readonly=True)
    target = sqlite3.connect(dst)
    source.backup(target)
    target.close()
    source.close()


def copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns(
                "node_modules",
                ".git",
                ".next",
                "dist",
                "build",
                ".venv",
                "__pycache__",
                ".pytest_cache",
            ),
            dirs_exist_ok=True,
        )
    else:
        shutil.copy2(src, dst)
    report(f"backed_up {src.name}")


def backup_metadata(codex_home: Path, backup_root: Path) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    for name in [
        ".codex-global-state.json",
        "config.toml",
        "history.jsonl",
        "installation_id",
        "models_cache.json",
        "session_index.jsonl",
        "version.json",
        "memories",
        "skills",
        "rules",
        "plugins",
        "automations",
    ]:
        copy_if_exists(codex_home / name, backup_root / name)
    sqlite_backup(codex_home / "state_5.sqlite", backup_root / "state_5.sqlite")


def load_pinned(codex_home: Path) -> set[str]:
    path = codex_home / ".codex-global-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("pinned-thread-ids", []))
    except Exception:
        return set()


def normalize_extended_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def normalize_extended_paths_in_text(value: str) -> str:
    return value.replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")


def extend_windows_path(value: str) -> str:
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    if WINDOWS_DRIVE_PATH_RE.match(value):
        return "\\\\?\\" + value
    return value


def normalize_path_text(value: str, *, style: str) -> str:
    if style == "extended":
        return extend_windows_path(normalize_extended_path(value))
    return normalize_extended_paths_in_text(value)


def normalized_path(value: str) -> Path:
    return Path(normalize_extended_path(value))


def is_path_column(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in PATH_COLUMN_HINTS)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'pragma table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return set()


def has_threads_columns(conn: sqlite3.Connection, required: set[str]) -> bool:
    return required.issubset(table_columns(conn, "threads"))


def active_unarchived_expr(columns: set[str]) -> str:
    return "COALESCE(archived,0)=0" if "archived" in columns else "archived_at is null"


def bounded_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def append_session_index_name(codex_home: Path, thread_id: str, name: str) -> None:
    path = codex_home / "session_index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": thread_id,
        "thread_name": name,
        "updated_at": utc_now_z(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def latest_session_index_name(codex_home: Path, thread_id: str) -> str | None:
    path = codex_home / "session_index.jsonl"
    if not path.exists():
        return None
    latest = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("id") == thread_id and record.get("thread_name"):
            latest = str(record["thread_name"])
    return latest


def should_append_repaired_session_index_name(
    codex_home: Path,
    item: ThreadMetadataRepair,
) -> bool:
    if not item.new_title or item.new_title == item.old_title:
        return False
    existing_name = latest_session_index_name(codex_home, item.thread_id)
    return existing_name is None or existing_name == item.old_title


def repaired_thread_title(codex_home: Path, thread_id: str, old_title: str, title_limit: int) -> str:
    existing_name = latest_session_index_name(codex_home, thread_id)
    if existing_name:
        return bounded_text(existing_name, title_limit)
    return bounded_text(old_title, title_limit)


def report_thread_metadata_bloat(
    conn: sqlite3.Connection,
    *,
    title_limit: int,
    preview_limit: int,
) -> None:
    columns = table_columns(conn, "threads")
    if not {"id", "title"}.issubset(columns):
        report("thread_metadata_bloat skipped_missing_threads_columns")
        return
    archived_expr = active_unarchived_expr(columns)
    preview_col = "first_user_message" if "first_user_message" in columns else None
    if preview_col:
        row = conn.execute(
            f"""
            select
              count(*),
              coalesce(sum(length(title)), 0),
              coalesce(sum(length(first_user_message)), 0),
              coalesce(max(length(title)), 0),
              coalesce(max(length(first_user_message)), 0),
              sum(case when length(title) > ? then 1 else 0 end),
              sum(case when length(first_user_message) > ? then 1 else 0 end),
              sum(case when length(first_user_message) > 10000 then 1 else 0 end)
            from threads
            where {archived_expr}
            """,
            (title_limit, preview_limit),
        ).fetchone()
        (
            active_rows,
            title_chars,
            preview_chars,
            max_title,
            max_preview,
            title_over_limit,
            preview_over_limit,
            preview_over_10k,
        ) = row
    else:
        row = conn.execute(
            f"""
            select
              count(*),
              coalesce(sum(length(title)), 0),
              coalesce(max(length(title)), 0),
              sum(case when length(title) > ? then 1 else 0 end)
            from threads
            where {archived_expr}
            """,
            (title_limit,),
        ).fetchone()
        active_rows, title_chars, max_title, title_over_limit = row
        preview_chars = max_preview = preview_over_limit = preview_over_10k = 0

    report(f"thread_active_rows {active_rows}")
    report(f"thread_title_chars {title_chars}")
    report(f"thread_first_user_message_chars {preview_chars}")
    report(f"thread_max_title_chars {max_title}")
    report(f"thread_max_first_user_message_chars {max_preview}")
    report(f"thread_titles_over_limit {title_over_limit or 0}")
    report(f"thread_first_user_message_over_limit {preview_over_limit or 0}")
    report(f"thread_first_user_message_over_10k {preview_over_10k or 0}")


def repair_thread_metadata_bloat(
    conn: sqlite3.Connection,
    codex_home: Path,
    backup_root: Path,
    *,
    apply: bool,
    details: bool,
    title_limit: int,
    preview_limit: int,
) -> None:
    required = {"id", "title"}
    if not has_threads_columns(conn, required):
        report("thread_metadata_repair skipped_missing_threads_columns")
        return
    columns = table_columns(conn, "threads")
    has_preview = "first_user_message" in columns
    archived_expr = active_unarchived_expr(columns)
    select_preview = "first_user_message" if has_preview else "''"
    rows = conn.execute(
        f"""
        select id, title, {select_preview}
        from threads
        where {archived_expr}
        """
    ).fetchall()

    repairs: list[ThreadMetadataRepair] = []
    for thread_id, title, preview in rows:
        thread_id = str(thread_id)
        old_title = title or ""
        old_preview = preview or ""
        new_title = repaired_thread_title(codex_home, thread_id, old_title, title_limit)
        new_preview = bounded_text(old_preview, preview_limit) if has_preview else ""
        if new_title != old_title or new_preview != old_preview:
            repairs.append(
                ThreadMetadataRepair(
                    thread_id,
                    old_title,
                    new_title,
                    old_preview,
                    new_preview,
                )
            )

    report(f"thread_metadata_repair_candidates {len(repairs)}")
    for index, item in enumerate(repairs[:10], start=1):
        label = f"thread_{index:03d}"
        title_delta = len(item.old_title) - len(item.new_title)
        preview_delta = len(item.old_preview) - len(item.new_preview)
        if details:
            report(
                f"thread_metadata_repair_candidate {label} thread_id={item.thread_id} "
                f"title_delta={title_delta} preview_delta={preview_delta}"
            )
        else:
            report(
                f"thread_metadata_repair_candidate {label} "
                f"title_delta={title_delta} preview_delta={preview_delta}"
            )

    if not apply or not repairs:
        return

    manifest = backup_root / "thread-metadata-repairs.jsonl"
    with manifest.open("w", encoding="utf-8") as handle:
        for item in repairs:
            record = {
                "thread_id": item.thread_id,
                "old_title": item.old_title,
                "new_title": item.new_title,
                "old_first_user_message": item.old_preview,
                "new_first_user_message": item.new_preview,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    cur = conn.cursor()
    for item in repairs:
        if has_preview:
            cur.execute(
                "update threads set title=?, first_user_message=? where id=?",
                (item.new_title, item.new_preview, item.thread_id),
            )
        else:
            cur.execute(
                "update threads set title=? where id=?",
                (item.new_title, item.thread_id),
            )
        if should_append_repaired_session_index_name(codex_home, item):
            append_session_index_name(codex_home, item.thread_id, item.new_title)
    report("thread_metadata_repair applied")
    report(f"thread_metadata_repair_manifest {manifest}")
    write_thread_metadata_restore_script(manifest, codex_home / "state_5.sqlite", backup_root)


def write_thread_metadata_restore_script(manifest: Path, state_db: Path, backup_root: Path) -> None:
    restore = backup_root / "restore-thread-metadata.py"
    restore.write_text(
        f'''#!/usr/bin/env python3
import json
import sqlite3
from pathlib import Path

manifest = Path(r"{manifest}")
db = Path(r"{state_db}")
conn = sqlite3.connect(db)
conn.execute("pragma busy_timeout=10000")
cols = {{row[1] for row in conn.execute('pragma table_info("threads")').fetchall()}}
has_preview = "first_user_message" in cols
for line in manifest.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        print(f"Skipping malformed manifest line: {{line[:80]}}", flush=True)
        continue
    if has_preview:
        conn.execute(
            "update threads set title=?, first_user_message=? where id=?",
            (rec["old_title"], rec["old_first_user_message"], rec["thread_id"]),
        )
    else:
        conn.execute(
            "update threads set title=? where id=?",
            (rec["old_title"], rec["thread_id"]),
        )
conn.commit()
conn.close()
''',
        encoding="utf-8",
    )
    restore.chmod(0o700)
    report(f"thread_metadata_restore_script {restore}")
    report(f"thread_metadata_restore_command {python_restore_command(restore)}")


def boolish(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def automation_targets_thread(definition: Path, thread_id: str) -> bool:
    try:
        for line in definition.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("target_thread_id"):
                continue
            _, _, value = stripped.partition("=")
            if value.strip().strip('"').lower() == thread_id.lower():
                return True
    except OSError:
        return False
    return False


def thread_automation_directories(root: Path, thread_id: str) -> list[Path]:
    automations = root / "automations"
    if not automations.exists():
        return []
    result: list[Path] = []
    for child in automations.iterdir():
        definition = child / "automation.toml"
        if child.is_dir() and definition.exists() and automation_targets_thread(definition, thread_id):
            result.append(child)
    return result


def backup_thread_automations(codex_home: Path, backup_root: Path, thread_ids: list[str]) -> list[ThreadAutomationBackup]:
    backups: list[ThreadAutomationBackup] = []
    backup_automation_root = backup_root / "thread_recovery_automations"
    seen: set[Path] = set()
    for thread_id in thread_ids:
        for source in thread_automation_directories(codex_home, thread_id):
            resolved = source.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            destination = backup_automation_root / source.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            backups.append(ThreadAutomationBackup(source.name, source, destination))
    return backups


def restore_missing_thread_automations(backups: list[ThreadAutomationBackup]) -> int:
    restored = 0
    for item in backups:
        if (item.source / "automation.toml").exists():
            continue
        if item.source.exists():
            shutil.rmtree(item.source)
        shutil.copytree(item.backup, item.source)
        restored += 1
    return restored


def count_thread_automations(codex_home: Path, thread_ids: list[str]) -> int:
    seen: set[Path] = set()
    for thread_id in thread_ids:
        for source in thread_automation_directories(codex_home, thread_id):
            seen.add(source.resolve())
    return len(seen)


def recover_thread_archive_state(
    conn: sqlite3.Connection,
    thread_ids: list[str],
    *,
    codex_home: Path,
    backup_root: Path,
    apply: bool,
    details: bool,
) -> None:
    columns = table_columns(conn, "threads")
    if "id" not in columns:
        report("codex_thread_recovery skipped_missing_threads_id")
        return
    if "archived" not in columns and "archived_at" not in columns:
        report("codex_thread_recovery skipped_missing_archive_columns")
        return

    title_expr = "title" if "title" in columns else "''"
    archived_expr = "archived" if "archived" in columns else "NULL"
    archived_at_expr = "archived_at" if "archived_at" in columns else "NULL"
    candidates: list[ThreadArchiveRefreshCandidate] = []
    missing: list[str] = []
    for thread_id in thread_ids:
        row = conn.execute(
            f"select id, {title_expr}, {archived_expr}, {archived_at_expr} from threads where id=?",
            (thread_id,),
        ).fetchone()
        if row is None:
            missing.append(thread_id)
            continue
        _, title, archived, archived_at = row
        candidates.append(
            ThreadArchiveRefreshCandidate(
                thread_id=thread_id,
                title=title or "",
                was_archived=boolish(archived) or archived_at is not None,
            )
        )

    report(f"codex_thread_recovery_candidates {len(candidates)}")
    for thread_id in missing:
        report(f"codex_thread_recovery_missing thread_id={thread_id}")
    for index, item in enumerate(candidates, start=1):
        label = f"thread_{index:03d}"
        state = "archived" if item.was_archived else "active"
        if details:
            report(
                f"codex_thread_recovery_candidate {label} thread_id={item.thread_id} "
                f"final_state={state} title={item.title[:70]}"
            )
        else:
            report(f"codex_thread_recovery_candidate {label} final_state={state}")

    if not apply or not candidates:
        return

    candidate_ids = [item.thread_id for item in candidates]
    automation_backups = backup_thread_automations(codex_home, backup_root, candidate_ids)
    report(f"codex_thread_recovery_automations {len(automation_backups)}")
    if details:
        for item in automation_backups:
            report(f"codex_thread_recovery_automation id={item.automation_id}")

    now = int(time.time())
    cur = conn.cursor()

    def set_archived_state(thread_id: str, archived: bool) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if "archived" in columns:
            assignments.append("archived=?")
            params.append(1 if archived else 0)
        if "archived_at" in columns:
            assignments.append("archived_at=?")
            params.append(now if archived else None)
        params.append(thread_id)
        cur.execute(f"update threads set {', '.join(assignments)} where id=?", params)

    for item in candidates:
        set_archived_state(item.thread_id, archived=True)
        set_archived_state(item.thread_id, archived=item.was_archived)
    restored = restore_missing_thread_automations(automation_backups)
    report(f"codex_thread_recovery_automations_restored {restored}")
    report(f"codex_thread_recovery_automations_after {count_thread_automations(codex_home, candidate_ids)}")
    report(f"codex_thread_recovery_applied {len(candidates)}")


BROKEN_THREAD_LOG_PREFIXES = (
    "failed to queue mcp refresh for thread ",
    "failed to start turn",
    "failed to update thread settings",
    "error creating task",
    "error submitting message",
    "failed to submit message",
)


def is_broken_thread_failure_log(body: object) -> bool:
    text = str(body or "").lstrip().lower()
    return any(text.startswith(prefix) for prefix in BROKEN_THREAD_LOG_PREFIXES)


def broken_thread_candidates(
    codex_home: Path,
    state_conn: sqlite3.Connection,
    *,
    lookback_hours: int,
) -> list[BrokenThreadCandidate]:
    logs_db = codex_home / "logs_2.sqlite"
    if not logs_db.exists():
        return []

    since = int(time.time()) - max(1, lookback_hours) * 60 * 60
    clauses = " or ".join(["lower(coalesce(feedback_log_body,'')) like ?" for _ in BROKEN_THREAD_LOG_PREFIXES])
    params: list[object] = [since]
    params.extend(f"{pattern}%" for pattern in BROKEN_THREAD_LOG_PREFIXES)
    try:
        logs_conn = sqlite_connect(logs_db, readonly=True)
        logs_conn.execute("pragma busy_timeout=1000")
        rows = logs_conn.execute(
            f"""
            select ts, thread_id, feedback_log_body
            from logs
            where ts >= ?
              and ({clauses})
            order by ts desc
            """,
            params,
        ).fetchall()
        logs_conn.close()
    except sqlite3.Error:
        return []

    grouped: dict[str, tuple[int, int]] = {}
    for ts, thread_id, body in rows:
        if not is_broken_thread_failure_log(body):
            continue
        ids = set(THREAD_REFERENCE_RE.findall(str(body or "")))
        if isinstance(thread_id, str) and THREAD_ID_RE.fullmatch(thread_id):
            ids.add(thread_id)
        for candidate_id in ids:
            current = grouped.get(candidate_id)
            count = 1 if current is None else current[0] + 1
            latest = int(ts or 0) if current is None else max(current[1], int(ts or 0))
            grouped[candidate_id] = (count, latest)

    if not grouped:
        return []

    columns = table_columns(state_conn, "threads")
    title_expr = "title" if "title" in columns else "''"
    archived_expr = "archived" if "archived" in columns else "NULL"
    archived_at_expr = "archived_at" if "archived_at" in columns else "NULL"
    updated_at_expr = "updated_at" if "updated_at" in columns else "NULL"
    recency_at_expr = "recency_at" if "recency_at" in columns else "NULL"
    candidates: list[BrokenThreadCandidate] = []
    for thread_id, (count, latest) in grouped.items():
        row = None
        if "id" in columns:
            row = state_conn.execute(
                f"select {title_expr}, {archived_expr}, {archived_at_expr}, {updated_at_expr}, {recency_at_expr} from threads where id=?",
                (thread_id,),
            ).fetchone()
        if row is None:
            candidates.append(
                BrokenThreadCandidate(
                    thread_id=thread_id,
                    title="",
                    failure_count=count,
                    last_seen=latest,
                    last_activity_at=None,
                    was_archived=None,
                )
            )
            continue
        title, archived, archived_at, updated_at, recency_at = row
        activity_values = [int(value) for value in (updated_at, recency_at) if value is not None]
        candidates.append(
            BrokenThreadCandidate(
                thread_id=thread_id,
                title=title or "",
                failure_count=count,
                last_seen=latest,
                last_activity_at=max(activity_values) if activity_values else None,
                was_archived=boolish(archived) or archived_at is not None,
            )
        )

    return sorted(candidates, key=lambda item: (item.last_seen, item.failure_count), reverse=True)


def report_broken_thread_candidates(candidates: list[BrokenThreadCandidate], *, details: bool) -> None:
    active_count = sum(1 for item in candidates if item.was_archived is False)
    recoverable_count = sum(1 for item in candidates if item.was_archived is False and item.is_current)
    stale_active_count = active_count - recoverable_count
    report(f"broken_thread_candidates {len(candidates)}")
    report(f"broken_thread_recoverable_candidates {recoverable_count}")
    report(f"thread_failure_log_candidates {len(candidates)}")
    report(f"thread_failure_log_recoverable_active_candidates {recoverable_count}")
    report(f"thread_failure_log_stale_active_candidates {stale_active_count}")
    for index, item in enumerate(candidates, start=1):
        label = f"thread_{index:03d}"
        state = "missing" if item.was_archived is None else "archived" if item.was_archived else "active"
        last_seen = datetime.fromtimestamp(item.last_seen, UTC).isoformat(timespec="seconds") if item.last_seen else "unknown"
        freshness = "current" if item.is_current else "stale_after_activity"
        if details:
            report(
                f"broken_thread_candidate {label} thread_id={item.thread_id} "
                f"failures={item.failure_count} last_seen={last_seen} state={state} freshness={freshness} title={item.title[:70]}"
            )
        else:
            report(f"broken_thread_candidate {label} failures={item.failure_count} last_seen={last_seen} state={state} freshness={freshness}")


def normalize_sqlite_paths(
    conn: sqlite3.Connection,
    apply: bool,
    *,
    style: str = "normal",
    active_threads_only: bool = False,
) -> int:
    cur = conn.cursor()
    total = 0
    tables = [
        row[0]
        for row in cur.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]
    if active_threads_only:
        tables = [table for table in tables if table == "threads"]
    for table in tables:
        cols = cur.execute(f'pragma table_info("{table}")').fetchall()
        column_names = {str(col[1]) for col in cols}
        text_cols = [
            col[1]
            for col in cols
            if ("TEXT" in (col[2] or "").upper() or col[2] == "") and is_path_column(str(col[1]))
        ]
        for col in text_cols:
            if style == "extended":
                where = f'"{col}" is not null'
                if active_threads_only and table == "threads":
                    where = f"{where} and {active_unarchived_expr(column_names)}"
                rows = cur.execute(
                    f'select rowid, "{col}" from "{table}" where {where}',
                ).fetchall()
            else:
                rows = cur.execute(
                    f'select rowid, "{col}" from "{table}" where instr("{col}", ?) > 0',
                    ("\\\\?\\",),
                ).fetchall()
            changed = 0
            for rowid, value in rows:
                if isinstance(value, str):
                    normalized = normalize_path_text(value, style=style)
                else:
                    normalized = value
                if normalized != value:
                    changed += 1
                    if apply:
                        cur.execute(
                            f'update "{table}" set "{col}"=? where rowid=?',
                            (normalized, rowid),
                        )
            if changed:
                report(f"extended_paths {table}.{col} {changed}")
                total += changed
    if total == 0:
        report("extended_paths 0")
    return total


def normalize_metadata_text_paths(codex_home: Path, apply: bool) -> int:
    total = 0
    for name in NORMALIZE_TEXT_FILES:
        path = codex_home / name
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        normalized = normalize_extended_paths_in_text(text)
        if normalized == text:
            continue
        count = text.count("\\\\?\\")
        total += count
        report(f"extended_paths_file {name} {count}")
        if apply:
            path.write_text(normalized, encoding="utf-8")
    if total == 0:
        report("extended_paths_file 0")
    return total


def hot_normalize_paths_once(codex_home: Path) -> int:
    state_db = codex_home / "state_5.sqlite"
    total = 0
    if state_db.exists():
        conn = sqlite_connect(state_db, readonly=False)
        conn.execute("pragma busy_timeout=10000")
        total += normalize_sqlite_paths(conn, True, style="extended", active_threads_only=True)
        conn.commit()
        conn.close()
    return total


def watch_hot_normalize_paths(codex_home: Path, *, seconds: int, interval_seconds: int) -> None:
    if seconds <= 0:
        return
    interval = max(1, interval_seconds)
    deadline = time.monotonic() + seconds
    iteration = 0
    report(f"hot_normalize_watch_start seconds={seconds} interval_seconds={interval}")
    while time.monotonic() < deadline:
        sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
        if sleep_for:
            time.sleep(sleep_for)
        iteration += 1
        changed = hot_normalize_paths_once(codex_home)
        report(f"hot_normalize_watch_iteration {iteration} changed={changed}")
    report("hot_normalize_watch_done")


def active_session_candidates(
    conn: sqlite3.Connection,
    codex_home: Path,
    archive_older_than_days: int,
    archive_age_field: str,
    archive_thread_ids: list[str],
    archive_rollout_paths: list[str],
) -> list[SessionCandidate]:
    sessions_root = codex_home / "sessions"
    sessions_root_canonical = canonical_path(sessions_root)
    cutoff = int(time.time() - archive_older_than_days * 24 * 60 * 60)
    pinned = load_pinned(codex_home)
    target_thread_ids = {value.lower() for value in archive_thread_ids}
    target_rollout_paths = {str(canonical_path(Path(value))) for value in archive_rollout_paths}
    columns = table_columns(conn, "threads")
    created_at_expr = "created_at" if "created_at" in columns else "NULL"
    active_expr = active_unarchived_expr(columns)
    rows = conn.execute(
        f"select id, title, rollout_path, {created_at_expr}, updated_at from threads where {active_expr}"
    ).fetchall()
    candidates_by_id: dict[str, SessionCandidate] = {}
    found_thread_ids: set[str] = set()
    found_rollout_paths: set[str] = set()
    for thread_id, title, rollout_path, created_at, updated_at in rows:
        thread_id = str(thread_id)
        if not rollout_path:
            continue
        source = normalized_path(rollout_path)
        canonical_source = str(canonical_path(source))
        targeted_by_thread = thread_id.lower() in target_thread_ids
        targeted_by_path = canonical_source in target_rollout_paths
        if targeted_by_thread:
            found_thread_ids.add(thread_id.lower())
        if targeted_by_path:
            found_rollout_paths.add(canonical_source)
        if thread_id in pinned:
            if targeted_by_thread or targeted_by_path:
                report(f"targeted_session_skipped_pinned thread_id={thread_id}")
            continue
        age_value = created_at if archive_age_field == "created_at" else updated_at
        reason = f"{archive_age_field}_older_than_{archive_older_than_days}d"
        if updated_at is not None and int(updated_at) >= cutoff:
            if not targeted_by_thread and not targeted_by_path and archive_age_field == "updated_at":
                continue
        if age_value is not None and int(age_value) >= cutoff and not targeted_by_thread and not targeted_by_path:
            continue
        if targeted_by_thread:
            reason = "target_thread_id"
        elif targeted_by_path:
            reason = "target_rollout_path"
        if not source.exists():
            if targeted_by_thread or targeted_by_path:
                report(f"targeted_session_skipped_missing_rollout thread_id={thread_id}")
            continue
        try:
            relative = canonical_path(source).relative_to(sessions_root_canonical)
        except ValueError:
            if targeted_by_thread or targeted_by_path:
                report(f"targeted_session_skipped_outside_sessions thread_id={thread_id}")
            continue
        candidates_by_id[thread_id] = SessionCandidate(
            source.stat().st_size,
            thread_id,
            title or "",
            source,
            relative,
            created_at,
            updated_at,
            reason,
        )
    for target in sorted(target_thread_ids - found_thread_ids):
        report(f"targeted_session_missing thread_id={target}")
    for target in sorted(target_rollout_paths - found_rollout_paths):
        report(f"targeted_session_missing rollout_path={target}")
    candidates = list(candidates_by_id.values())
    candidates.sort(key=lambda item: item.size, reverse=True)
    return candidates


def malformed_local_task_reason(cwd: str) -> str | None:
    if cwd == "/":
        return "root_cwd"
    if TEMP_LOCAL_TASK_CWD_RE.search(cwd):
        return "temp_cwd"
    return None


def malformed_local_task_candidates(
    conn: sqlite3.Connection,
    codex_home: Path,
) -> list[MalformedLocalTaskCandidate]:
    columns = table_columns(conn, "threads")
    required = {"id", "title", "rollout_path", "cwd", "updated_at", "has_user_event"}
    missing = required - columns
    if missing:
        report(f"malformed_local_task_skipped_missing_columns {','.join(sorted(missing))}")
        return []

    sessions_root = codex_home / "sessions"
    sessions_root_canonical = canonical_path(sessions_root)
    pinned = load_pinned(codex_home)
    active_expr = active_unarchived_expr(columns)
    rows = conn.execute(
        f"""
        select id, title, rollout_path, cwd, updated_at
        from threads
        where {active_expr}
          and COALESCE(has_user_event,0)=0
          and rollout_path <> ''
        """
    ).fetchall()

    candidates: list[MalformedLocalTaskCandidate] = []
    for thread_id, title, rollout_path, cwd, updated_at in rows:
        if thread_id in pinned:
            continue
        reason = malformed_local_task_reason(cwd or "")
        if reason is None:
            continue
        source = normalized_path(rollout_path)
        if not source.exists():
            continue
        try:
            relative = canonical_path(source).relative_to(sessions_root_canonical)
        except ValueError:
            continue
        candidates.append(
            MalformedLocalTaskCandidate(
                source.stat().st_size,
                thread_id,
                title or "",
                cwd or "",
                reason,
                source,
                relative,
                updated_at,
            )
        )
    candidates.sort(key=lambda item: item.size, reverse=True)
    return candidates


def archive_thread_row(
    cur: sqlite3.Cursor,
    columns: set[str],
    *,
    rollout_path: str,
    archived_at: int,
    thread_id: str,
) -> None:
    if "archived" in columns:
        cur.execute(
            "update threads set rollout_path=?, archived=1, archived_at=? where id=?",
            (rollout_path, archived_at, thread_id),
        )
    else:
        cur.execute(
            "update threads set rollout_path=?, archived_at=? where id=?",
            (rollout_path, archived_at, thread_id),
        )


def archive_sessions(
    conn: sqlite3.Connection,
    candidates: list[SessionCandidate],
    codex_home: Path,
    backup_root: Path,
    stamp: str,
    apply: bool,
    details: bool,
) -> None:
    total = sum(item.size for item in candidates)
    report(f"old_session_candidates {len(candidates)}")
    report(f"old_session_candidate_gb {gb(total)}")
    for index, item in enumerate(candidates[:10], start=1):
        label = f"session_{index:03d}"
        if details:
            report(
                f"large_session_mb {mb(item.size)} {label} thread_id={item.thread_id} "
                f"reason={item.reason} title={item.title[:70]}"
            )
        else:
            report(f"large_session_mb {mb(item.size)} {label}")
    if not apply or not candidates:
        return

    archive_root = codex_home / "archived_sessions" / f"keep-codex-fast-{stamp}"
    manifest = backup_root / "moved-sessions.jsonl"
    archive_root.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    cur = conn.cursor()
    columns = table_columns(conn, "threads")
    with manifest.open("w", encoding="utf-8") as handle:
        for item in candidates:
            dest = archive_root / item.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item.source), str(dest))
            record = {
                "thread_id": item.thread_id,
                "bytes": item.size,
                "from": str(item.source),
                "to": str(dest),
                "reason": item.reason,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            archive_thread_row(cur, columns, rollout_path=str(dest), archived_at=now, thread_id=item.thread_id)
    write_session_restore_script(manifest, codex_home / "state_5.sqlite", backup_root)
    report(f"archived_sessions_root {archive_root}")
    report(f"archived_sessions_manifest {manifest}")


def archive_malformed_local_tasks(
    conn: sqlite3.Connection,
    candidates: list[MalformedLocalTaskCandidate],
    codex_home: Path,
    backup_root: Path,
    stamp: str,
    apply: bool,
    details: bool,
) -> None:
    total = sum(item.size for item in candidates)
    root_count = sum(1 for item in candidates if item.reason == "root_cwd")
    temp_count = sum(1 for item in candidates if item.reason == "temp_cwd")
    report(f"malformed_local_task_candidates {len(candidates)}")
    report(f"malformed_local_task_candidate_gb {gb(total)}")
    report(f"malformed_local_task_root_cwd {root_count}")
    report(f"malformed_local_task_temp_cwd {temp_count}")
    for index, item in enumerate(candidates[:10], start=1):
        label = f"malformed_task_{index:03d}"
        if details:
            report(
                f"malformed_local_task_candidate {label} thread_id={item.thread_id} "
                f"reason={item.reason} cwd={item.cwd} title={item.title[:70]}"
            )
        else:
            report(f"malformed_local_task_candidate {label} reason={item.reason}")
    if not apply or not candidates:
        return

    archive_root = codex_home / "archived_sessions" / f"malformed-local-tasks-{stamp}"
    manifest = backup_root / "moved-malformed-local-tasks.jsonl"
    archive_root.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    cur = conn.cursor()
    columns = table_columns(conn, "threads")
    with manifest.open("w", encoding="utf-8") as handle:
        for item in candidates:
            dest = archive_root / item.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item.source), str(dest))
            record = {
                "thread_id": item.thread_id,
                "bytes": item.size,
                "reason": item.reason,
                "cwd": item.cwd,
                "from": str(item.source),
                "to": str(dest),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            archive_thread_row(cur, columns, rollout_path=str(dest), archived_at=now, thread_id=item.thread_id)
    write_session_restore_script(
        manifest,
        codex_home / "state_5.sqlite",
        backup_root,
        restore_name="restore-malformed-local-tasks.py",
    )
    report(f"malformed_local_task_archive_root {archive_root}")
    report(f"malformed_local_task_manifest {manifest}")


def write_session_restore_script(
    manifest: Path,
    state_db: Path,
    backup_root: Path,
    *,
    restore_name: str = "restore-sessions.py",
) -> None:
    restore = backup_root / restore_name
    restore.write_text(
        f'''#!/usr/bin/env python3
import json
import shutil
import sqlite3
from pathlib import Path

manifest = Path(r"{manifest}")
db = Path(r"{state_db}")
conn = sqlite3.connect(db)
conn.execute("pragma busy_timeout=10000")
columns = {{row[1] for row in conn.execute('pragma table_info("threads")')}}
for line in manifest.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        print(f"Skipping malformed manifest line: {{line[:80]}}", flush=True)
        continue
    src = Path(rec["to"])
    dest = Path(rec["from"])
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    if rec.get("thread_id"):
        if "archived" in columns:
            conn.execute(
                "update threads set rollout_path=?, archived=0, archived_at=NULL where id=?",
                (str(dest), rec["thread_id"]),
            )
        else:
            conn.execute(
                "update threads set rollout_path=?, archived_at=NULL where id=?",
                (str(dest), rec["thread_id"]),
            )
conn.commit()
conn.close()
''',
        encoding="utf-8",
    )
    restore.chmod(0o700)
    report(f"session_restore_script {restore}")
    report(f"session_restore_command {python_restore_command(restore)}")


def prune_config(codex_home: Path, backup_root: Path, apply: bool, write_artifacts: bool) -> None:
    path = codex_home / "config.toml"
    if not path.exists():
        report("config_prune_candidates 0")
        return
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    out: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = PROJECT_HEADER_RE.match(line)
        if not match:
            out.append(line)
            i += 1
            continue
        project_path = match.group(2)
        block = [line]
        i += 1
        while i < len(lines) and not lines[i].startswith("["):
            block.append(lines[i])
            i += 1
        should_remove = bool(TEMP_PROJECT_RE.search(project_path)) or not Path(project_path).exists()
        if should_remove:
            removed.append(project_path)
        else:
            out.extend(block)

    if write_artifacts:
        (backup_root / "pruned-projects.txt").write_text(
            "\n".join(removed) + ("\n" if removed else ""),
            encoding="utf-8",
        )
    report(f"config_prune_candidates {len(removed)}")
    if apply and removed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        report("config_pruned applied")


def move_stale_worktrees(codex_home: Path, backup_root: Path, days: int, stamp: str, apply: bool) -> None:
    root = codex_home / "worktrees"
    if not root.exists():
        report("worktree_candidates 0")
        return
    cutoff = time.time() - days * 24 * 60 * 60
    candidates = [path for path in root.iterdir() if path.is_dir() and path.stat().st_mtime < cutoff]
    total = sum(size_bytes(path) for path in candidates)
    report(f"worktree_candidates {len(candidates)}")
    report(f"worktree_candidate_gb {gb(total)}")
    if not apply or not candidates:
        return
    archive_root = codex_home / "archived_worktrees" / f"keep-codex-fast-{stamp}"
    manifest = backup_root / "moved-worktrees.jsonl"
    archive_root.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8") as handle:
        for source in candidates:
            dest = archive_root / source.name
            item_size = size_bytes(source)
            shutil.move(str(source), str(dest))
            handle.write(json.dumps({"from": str(source), "to": str(dest), "bytes": item_size}) + "\n")
    write_worktree_restore_script(manifest, backup_root)
    report(f"worktree_archive_root {archive_root}")
    report(f"worktree_manifest {manifest}")


def write_worktree_restore_script(manifest: Path, backup_root: Path) -> None:
    restore = backup_root / "restore-worktrees.py"
    restore.write_text(
        f'''#!/usr/bin/env python3
import json
import shutil
from pathlib import Path

manifest = Path(r"{manifest}")
for line in manifest.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        print(f"Skipping malformed manifest line: {{line[:80]}}", flush=True)
        continue
    src = Path(rec["to"])
    dest = Path(rec["from"])
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
''',
        encoding="utf-8",
    )
    restore.chmod(0o700)
    report(f"worktree_restore_script {restore}")
    report(f"worktree_restore_command {python_restore_command(restore)}")


def rotate_logs(codex_home: Path, threshold_mb: int, stamp: str, apply: bool) -> None:
    sqlite_logs = [path for path in codex_home.glob("logs_2.sqlite*") if path.is_file()]
    tui_log = codex_home / "log" / "codex-tui.log"
    files = sqlite_logs + ([tui_log] if tui_log.is_file() else [])
    sqlite_total = sum(path.stat().st_size for path in sqlite_logs)
    tui_total = tui_log.stat().st_size if tui_log.is_file() else 0
    total = sqlite_total + tui_total
    report(f"logs_mb {mb(total)}")
    report(f"logs_2_sqlite_mb {mb(sqlite_total)}")
    report(f"codex_tui_log_mb {mb(tui_total)}")
    if total < threshold_mb * 1024 * 1024:
        report("logs_rotate skipped_below_threshold")
        return
    if apply and files:
        archive_root = codex_home / "archived_logs" / f"keep-codex-fast-{stamp}"
        archive_root.mkdir(parents=True, exist_ok=True)
        for path in files:
            dest_name = "codex-tui.log" if path == tui_log else path.name
            shutil.move(str(path), str(archive_root / dest_name))
        report(f"logs_archive_root {archive_root}")


def top_node_processes(details: bool) -> None:
    system = platform.system()
    report("top_node_processes")
    try:
        if system == "Windows":
            command = (
                "Get-Process node -ErrorAction SilentlyContinue | "
                "Sort-Object WorkingSet64 -Descending | Select-Object -First 10 "
                "Id,ProcessName,@{n='MB';e={[math]::Round($_.WorkingSet64/1MB,1)}},Path | "
                "ConvertTo-Json -Compress"
            )
            output = subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True)
            if not output.strip():
                return
            data = json.loads(output)
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                if details:
                    report(f"node_mb {row.get('MB')} pid={row.get('Id')} path={row.get('Path')}")
                else:
                    report(f"node_mb {row.get('MB')} process=node")
            return
        output = subprocess.check_output(["ps", "-axo", "pid=,rss=,comm=,args="], text=True)
        rows = []
        for line in output.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) >= 3 and "node" in parts[2].lower():
                rows.append((int(parts[1]), line.strip()))
        for rss, line in sorted(rows, reverse=True)[:10]:
            if details:
                report(f"node_mb {rss / 1024:.1f} {line}")
            else:
                report(f"node_mb {rss / 1024:.1f} process=node")
    except Exception as exc:
        report(f"node_process_report_skipped {exc}")


def verify_sizes(codex_home: Path) -> None:
    for rel in ["sessions", "archived_sessions", "worktrees", "archived_worktrees", "archived_logs"]:
        path = codex_home / rel
        if path.exists():
            report(f"size_{rel}_gb {gb(size_bytes(path))}")


def run(args: argparse.Namespace) -> int:
    codex_home = codex_home_from_args(args.codex_home)
    if not codex_home.exists():
        report(f"codex_home_missing {codex_home}")
        return 2

    recovery_thread_ids = list(getattr(args, "recover_thread_id", []))
    recover_detected_threads = bool(getattr(args, "recover_detected_threads", False))
    recovery_mode = bool(recovery_thread_ids or recover_detected_threads)
    stamp = now_stamp()
    backup_root = Path(args.backup_root).expanduser() if args.backup_root else documents_backup_root() / f"keep-codex-fast-{stamp}"
    backup_root = backup_root.resolve()

    running = codex_processes_running()
    hot_normalize_paths = bool(getattr(args, "hot_normalize_paths", False))

    if args.apply and running and args.wait_for_codex_exit and not recovery_mode and not hot_normalize_paths:
        report("waiting_for_codex_exit")
        wait_for_codex_exit()
        running = []

    effective_apply = bool(args.apply and (not running or recovery_mode))
    hot_path_apply = bool(args.apply and running and hot_normalize_paths and not recovery_mode)
    path_apply = bool(effective_apply or hot_path_apply)
    effective_backup = bool(effective_apply or hot_path_apply or args.backup_only)
    requested_mode = "apply" if args.apply else "backup-only" if args.backup_only else "report"
    effective_mode = "hot-normalize-paths" if hot_path_apply else "apply" if effective_apply else "backup-only" if effective_backup else "report"
    if args.details:
        report(f"codex_home {codex_home}")
        if effective_backup:
            report(f"backup_root {backup_root}")
    elif effective_backup:
        report(f"backup_root {backup_root}")
    report(f"requested_mode {requested_mode}")
    report(f"effective_mode {effective_mode}")
    if effective_mode == "report":
        report("mode_safety read_only=true privacy=pseudonymous")
    elif effective_mode == "backup-only":
        report("mode_safety backup_only=true archives=false state_writes=false")
    elif effective_mode == "apply":
        report("mode_safety backup_first=true archive_only=true permanent_delete=false")
    else:
        report("mode_safety backup_first=true hot_path_normalization_only=true archives=false logs=false worktrees=false metadata_repair=false")
    if args.apply and running and not recovery_mode and not hot_path_apply:
        report("apply_skipped_codex_running")
        for index, proc in enumerate(running, start=1):
            if args.details:
                report(f"blocking_process {proc}")
            else:
                report(f"blocking_process codex_process_{index:03d}")
    elif hot_path_apply:
        report("hot_normalize_paths_codex_running")
    if recovery_mode:
        report("codex_thread_recovery_mode targeted=true broad_cleanup=false")

    if effective_backup:
        backup_metadata(codex_home, backup_root)

    state_db = codex_home / "state_5.sqlite"
    if state_db.exists():
        conn = sqlite_connect(state_db, readonly=not path_apply)
        conn.execute("pragma busy_timeout=10000")
        detected_broken_threads = broken_thread_candidates(
            codex_home,
            conn,
            lookback_hours=getattr(args, "broken_thread_lookback_hours", 48),
        )
        report_broken_thread_candidates(detected_broken_threads, details=args.details)
        if recover_detected_threads:
            detected_ids = [
                item.thread_id
                for item in detected_broken_threads
                if item.was_archived is False and item.is_current
            ][: getattr(args, "max_recover_detected_threads", 20)]
            seen = set(recovery_thread_ids)
            recovery_thread_ids.extend(thread_id for thread_id in detected_ids if thread_id not in seen)
        if recovery_mode:
            recover_thread_archive_state(
                conn,
                recovery_thread_ids,
                codex_home=codex_home,
                backup_root=backup_root,
                apply=effective_apply,
                details=args.details,
            )
            if effective_apply:
                conn.commit()
            conn.close()
            report("done")
            return 0

        path_style = "extended" if hot_path_apply else "normal"
        normalize_sqlite_paths(
            conn,
            path_apply,
            style=path_style,
            active_threads_only=hot_path_apply,
        )
        report_thread_metadata_bloat(
            conn,
            title_limit=args.thread_title_limit,
            preview_limit=args.thread_preview_limit,
        )
        repair_thread_metadata_bloat(
            conn,
            codex_home,
            backup_root,
            apply=effective_apply and args.repair_thread_metadata_bloat,
            details=args.details,
            title_limit=args.thread_title_limit,
            preview_limit=args.thread_preview_limit,
        )
        malformed_candidates = malformed_local_task_candidates(conn, codex_home)
        archive_malformed_local_tasks(
            conn,
            malformed_candidates,
            codex_home,
            backup_root,
            stamp,
            effective_apply and getattr(args, "archive_malformed_local_tasks", False),
            args.details,
        )
        if effective_apply and malformed_candidates and not getattr(args, "archive_malformed_local_tasks", False):
            report("malformed_local_task_archive skipped_flag_required")
        candidates = active_session_candidates(
            conn,
            codex_home,
            args.archive_older_than_days,
            args.archive_age_field,
            args.archive_thread_id,
            args.archive_rollout_path,
        )
        archive_sessions(conn, candidates, codex_home, backup_root, stamp, effective_apply, args.details)
        if path_apply:
            conn.commit()
        if effective_apply:
            try:
                conn.execute("pragma wal_checkpoint(truncate)")
            except Exception as exc:
                report(f"wal_checkpoint_skipped {exc}")
            try:
                conn.execute("pragma optimize")
            except Exception as exc:
                report(f"sqlite_optimize_skipped {exc}")
        conn.close()
    else:
        report("state_db_missing")

    normalize_metadata_text_paths(codex_home, path_apply and not hot_path_apply)
    prune_config(codex_home, backup_root, effective_apply, effective_backup)
    move_stale_worktrees(codex_home, backup_root, args.worktree_older_than_days, stamp, effective_apply)
    rotate_logs(codex_home, args.rotate_logs_above_mb, stamp, effective_apply)
    if hot_path_apply:
        watch_hot_normalize_paths(
            codex_home,
            seconds=max(0, int(getattr(args, "hot_normalize_watch_seconds", 0))),
            interval_seconds=max(1, int(getattr(args, "hot_normalize_interval_seconds", 30))),
        )
    verify_sizes(codex_home)
    top_node_processes(args.details)
    report("done")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safe, backup-first, archive-only Codex local-state maintenance."
    )
    parser.add_argument("--apply", action="store_true", help="Apply maintenance actions. Default is report-only.")
    parser.add_argument(
        "--hot-normalize-paths",
        action="store_true",
        help=(
            "With --apply, back up and normalize Windows extended paths even while Codex is running. "
            "Only path normalization runs; archiving, log rotation, worktree moves, and metadata repair stay disabled."
        ),
    )
    parser.add_argument(
        "--hot-normalize-watch-seconds",
        type=int,
        default=0,
        help="With --apply --hot-normalize-paths, keep repeating path normalization for this many seconds after the first pass.",
    )
    parser.add_argument(
        "--hot-normalize-interval-seconds",
        type=int,
        default=30,
        help="Interval between repeated hot path normalization passes. Default: 30.",
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Create backups without applying maintenance actions. Default report mode writes no files.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Include raw thread IDs, titles, paths, and process paths in output.",
    )
    parser.add_argument("--wait-for-codex-exit", action="store_true", help="Wait until Codex exits before applying.")
    parser.add_argument("--codex-home", help="Override Codex home. Defaults to CODEX_HOME or ~/.codex.")
    parser.add_argument("--backup-root", help="Override backup output folder.")
    parser.add_argument("--archive-older-than-days", type=int, default=10)
    parser.add_argument(
        "--archive-age-field",
        choices=["updated_at", "created_at"],
        default="updated_at",
        help="Timestamp field used by --archive-older-than-days. Defaults to updated_at.",
    )
    parser.add_argument(
        "--archive-thread-id",
        action="append",
        default=[],
        help="Archive one active session by thread id, bypassing the age threshold. Can be repeated.",
    )
    parser.add_argument(
        "--archive-rollout-path",
        action="append",
        default=[],
        help="Archive one active session by rollout JSONL path, bypassing the age threshold. Can be repeated.",
    )
    parser.add_argument(
        "--recover-thread-id",
        action="append",
        default=[],
        help=(
            "Refresh one stuck thread by toggling its archived state and restoring the original final state. "
            "Use with --apply after a backup is created. Can be repeated."
        ),
    )
    parser.add_argument(
        "--recover-detected-threads",
        action="store_true",
        help=(
            "With --apply, refresh recent broken-thread candidates detected in logs_2.sqlite. "
            "Report mode only lists candidates."
        ),
    )
    parser.add_argument(
        "--broken-thread-lookback-hours",
        type=int,
        default=48,
        help="How many hours of logs_2.sqlite to inspect for broken-thread signatures. Default: 48.",
    )
    parser.add_argument(
        "--max-recover-detected-threads",
        type=int,
        default=20,
        help="Maximum detected thread candidates to recover with --recover-detected-threads. Default: 20.",
    )
    parser.add_argument("--worktree-older-than-days", type=int, default=7)
    parser.add_argument(
        "--rotate-logs-above-mb",
        type=int,
        default=64,
        help="Archive logs_2.sqlite* and log/codex-tui.log when their combined size is at least this many MB.",
    )
    parser.add_argument(
        "--thread-title-limit",
        type=int,
        default=DEFAULT_TITLE_LIMIT,
        help="Title length threshold for metadata-bloat reporting and optional repair.",
    )
    parser.add_argument(
        "--thread-preview-limit",
        type=int,
        default=DEFAULT_PREVIEW_LIMIT,
        help="Preview length threshold for metadata-bloat reporting and optional repair.",
    )
    parser.add_argument(
        "--repair-thread-metadata-bloat",
        action="store_true",
        help="With --apply, trim oversized thread title/preview metadata. Default --apply only reports candidates.",
    )
    parser.add_argument(
        "--archive-malformed-local-tasks",
        action="store_true",
        help=(
            "With --apply, archive active no-user-event local task sessions with suspicious cwd values "
            "such as / or OS temp folders. Default --apply only reports candidates."
        ),
    )
    args = parser.parse_args(argv)
    if args.apply and args.backup_only:
        parser.error("--apply and --backup-only cannot be used together")
    for thread_id in args.archive_thread_id:
        if not THREAD_ID_RE.fullmatch(thread_id):
            parser.error(f"--archive-thread-id must be a UUID-like thread id: {thread_id}")
    for thread_id in args.recover_thread_id:
        if not THREAD_ID_RE.fullmatch(thread_id):
            parser.error(f"--recover-thread-id must be a UUID-like thread id: {thread_id}")
    if args.recover_detected_threads and not args.apply:
        parser.error("--recover-detected-threads requires --apply")
    if args.broken_thread_lookback_hours < 1:
        parser.error("--broken-thread-lookback-hours must be at least 1")
    if args.max_recover_detected_threads < 1:
        parser.error("--max-recover-detected-threads must be at least 1")
    if args.archive_older_than_days < 0:
        parser.error("--archive-older-than-days must be non-negative")
    if args.thread_title_limit < 20:
        parser.error("--thread-title-limit must be at least 20")
    if args.thread_preview_limit < args.thread_title_limit:
        parser.error("--thread-preview-limit must be greater than or equal to --thread-title-limit")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
