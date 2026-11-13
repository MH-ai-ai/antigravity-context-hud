"""Core data discovery and context usage parsing for the Antigravity Context HUD."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

import context_parser as default_parser


DEFAULT_CONVERSATIONS_DIR = Path.home() / ".gemini" / "antigravity" / "conversations"


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    db_path: Path
    latest_mtime_ns: int
    title: str


@dataclass(frozen=True)
class ContextSnapshot:
    conversation_id: str
    title: str
    model: str
    model_id: str
    used_tokens: int
    limit_tokens: int
    used_pct: float
    free_tokens: int
    request_budget_tokens: int
    heading: str
    compacted: bool
    has_breakdown: bool
    checkpoint_buffer: int
    grid_segments: list[int]
    categories: list[dict[str, Any]]
    checkpoint_view: dict[str, Any] | None
    fmt_used: str
    fmt_limit: str
    fmt_free: str
    fmt_budget: str
    chat_tokens: int
    tool_tokens: int
    system_total_tokens: int
    file_counts: dict[str, int]


def db_activity_mtime_ns(db_path: Path) -> int:
    """Return latest mtime_ns between .db and .db-wal (ignoring .db-shm to avoid read-only touch)."""
    latest = 0
    for candidate in (db_path, Path(f"{db_path}-wal")):
        try:
            st = candidate.stat()
            if st.st_mtime_ns > latest:
                latest = st.st_mtime_ns
        except OSError:
            continue
    return latest


def db_stamp(db_path: Path, catalog_path: Path | None = None) -> tuple[Any, ...]:
    """Return a cheap filesystem stamp to detect conversation or catalog changes."""
    items: list[tuple[int, int]] = []
    paths = [db_path, Path(f"{db_path}-wal")]
    if catalog_path is not None:
        paths.append(catalog_path)
    for p in paths:
        try:
            st = p.stat()
            items.append((st.st_mtime_ns, st.st_size))
        except OSError:
            items.append((0, 0))
    return tuple(items)


def list_conversation_dbs(conversations_dir: Path = DEFAULT_CONVERSATIONS_DIR) -> list[tuple[str, Path, int]]:
    """List (conversation_id, db_path, latest_mtime_ns) sorted from newest to oldest."""
    if not conversations_dir.is_dir():
        return []
    found: list[tuple[str, Path, int]] = []
    for p in conversations_dir.glob("*.db"):
        mtime_ns = db_activity_mtime_ns(p)
        if mtime_ns > 0:
            found.append((p.stem, p, mtime_ns))
    found.sort(key=lambda item: item[2], reverse=True)
    return found


def extract_conversation_title(steps: list[Any], cr_module: Any, conv_id: str, max_len: int = 26) -> str:
    """Extract a clean one-line title from the first non-empty user message."""
    for step in steps:
        if getattr(step, "type", None) != 14:  # USER_INPUT
            continue
        ui = step.payload(cr_module.STEP_USER_INPUT)
        if ui is None:
            continue
        text = " ".join(ui.str(2).split())
        if text:
            return text if len(text) <= max_len else text[: max_len - 1] + "…"
    return f"会话 {conv_id[:8]}"


def inspect_conversation_meta(db_path: Path, cr_module: Any) -> tuple[bool, str]:
    """Check whether DB is a user-initiated conversation and extract its title."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT idx, step_type, status, metadata, step_payload FROM steps "
            "WHERE step_type = 14 ORDER BY idx LIMIT 5"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return False, f"后台会话 {db_path.stem[:8]}"
    for pos, row in enumerate(rows):
        step = cr_module.Step(pos, row[4] or b"", row[1], row[2], row[3])
        title = extract_conversation_title([step], cr_module, db_path.stem)
        if not title.startswith("会话 "):
            return True, title
    return True, f"会话 {db_path.stem[:8]}"


class ContextMonitorService:
    """Stateful, low-overhead reader for Antigravity desktop conversation context."""

    def __init__(
        self,
        conversations_dir: Path = DEFAULT_CONVERSATIONS_DIR,
        catalog_path: Path | None = None,
        report_module: Any = None,
    ) -> None:
        self.conversations_dir = conversations_dir
        self.catalog_path = catalog_path
        self.cr = report_module if report_module is not None else default_parser
        self._meta_cache: dict[str, tuple[int, bool, str]] = {}
        self._snapshot_cache: dict[str, tuple[tuple[Any, ...], ContextSnapshot]] = {}
        self._catalog_cache: tuple[tuple[int, int], dict[str, Any]] = ((0, 0), {})

    def _load_catalog(self) -> dict[str, Any]:
        if self.catalog_path is None:
            return {}
        try:
            st = self.catalog_path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return {}
        if stamp == self._catalog_cache[0]:
            return self._catalog_cache[1]
        try:
            data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._catalog_cache = (stamp, data)
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _get_meta(self, conv_id: str, db_path: Path, mtime_ns: int) -> tuple[bool, str]:
        cached = self._meta_cache.get(conv_id)
        if cached and cached[0] == mtime_ns:
            return cached[1], cached[2]
        try:
            is_user, title = inspect_conversation_meta(db_path, self.cr)
        except sqlite3.Error:
            is_user, title = False, f"会话 {conv_id[:8]}"
        self._meta_cache[conv_id] = (mtime_ns, is_user, title)
        return is_user, title

    def list_recent_conversations(self, limit: int = 8) -> list[ConversationSummary]:
        items = list_conversation_dbs(self.conversations_dir)
        summaries: list[ConversationSummary] = []
        for conv_id, db_path, mtime_ns in items:
            is_user, title = self._get_meta(conv_id, db_path, mtime_ns)
            if not is_user:
                continue
            summaries.append(ConversationSummary(conv_id, db_path, mtime_ns, title))
            if len(summaries) >= limit:
                break
        return summaries

    def resolve_target(self, pinned_conversation_id: str | None = None) -> Path | None:
        if pinned_conversation_id:
            candidate = self.conversations_dir / f"{pinned_conversation_id}.db"
            if candidate.is_file():
                return candidate
        items = list_conversation_dbs(self.conversations_dir)
        for conv_id, db_path, mtime_ns in items:
            is_user, _ = self._get_meta(conv_id, db_path, mtime_ns)
            if is_user:
                return db_path
        return items[0][1] if items else None

    def get_snapshot(self, pinned_conversation_id: str | None = None) -> ContextSnapshot | None:
        db_path = self.resolve_target(pinned_conversation_id)
        if db_path is None:
            return None

        conv_id = db_path.stem
        stamp = db_stamp(db_path, self.catalog_path)
        cached = self._snapshot_cache.get(conv_id)
        if cached and cached[0] == stamp:
            return cached[1]

        steps, gen = self.cr.load(db_path)
        overrides = self._load_catalog()
        slug = self.cr.model_id(gen)
        catalog_entry = self.cr.lookup_model_catalog(slug, overrides)
        limit = self.cr.context_limit(gen, catalog_entry)
        model = catalog_entry.get("displayName") or self.cr.model_name(gen)
        st = self.cr.collect_stats(steps, gen, limit)

        used = int(st["total"])
        free = limit - used if used < limit else 0
        budget = int(self.cr.request_budget(gen) or 0)
        title = extract_conversation_title(steps, self.cr, conv_id)
        is_user = any(getattr(s, "type", None) == 14 for s in steps)
        self._meta_cache[conv_id] = (db_activity_mtime_ns(db_path), is_user, title)

        categories = [
            {
                "icon": icon,
                "name": name,
                "key": key,
                "tokens": int(st[key]),
                "pct": round(self.cr.pct(st[key], limit), 1),
            }
            for icon, name, key, always in self.cr.CATEGORIES
            if always or st[key]
        ]

        chat_tok = int(st["user"] + st["agent"] + st["system_messages"])
        tool_tok = int(st["tool"])
        system_total_tok = max(0, used - chat_tok - tool_tok)
        sys_files = self.cr.detect_system_files(str(Path.home()), None, "antigravity")

        snapshot = ContextSnapshot(
            conversation_id=conv_id,
            title=title,
            model=model,
            model_id=slug,
            used_tokens=used,
            limit_tokens=limit,
            used_pct=round(min(100.0, self.cr.pct(used, limit)), 1) if limit else 0.0,
            free_tokens=free,
            request_budget_tokens=budget,
            heading=self.cr.stats_heading(st),
            compacted=bool(st["compacted"]),
            has_breakdown=bool(st["has_breakdown"]),
            checkpoint_buffer=int(st["checkpoint"]),
            grid_segments=list(self.cr.grid_segments(st)),
            categories=categories,
            checkpoint_view=self.cr.checkpoint_view(st),
            fmt_used=self.cr.fmt_tok(used),
            fmt_limit=self.cr.fmt_ctx_limit(limit) if limit else "?",
            fmt_free=self.cr.fmt_tok(free),
            fmt_budget=self.cr.fmt_tok(budget) if budget else "",
            chat_tokens=chat_tok,
            tool_tokens=tool_tok,
            system_total_tokens=system_total_tok,
            file_counts={
                "knowledge": len(st["knowledge_files"]),
                "artifacts": len(st["artifact_files"]),
                "skills": len(st["skill_files"]),
                "system": len(sys_files),
            },
        )
        self._snapshot_cache[conv_id] = (stamp, snapshot)
        return snapshot
