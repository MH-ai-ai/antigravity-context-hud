"""Unit tests for the standalone Antigravity Context HUD core & parser."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import context_parser as cp  # noqa: E402
import core  # noqa: E402


def _pb_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_int(fn: int, val: int) -> bytes:
    return _pb_varint((fn << 3) | 0) + _pb_varint(val)


def _pb_bytes(fn: int, data: bytes) -> bytes:
    return _pb_varint((fn << 3) | 2) + _pb_varint(len(data)) + data


def _pb_str(fn: int, text: str) -> bytes:
    return _pb_bytes(fn, text.encode("utf-8"))


def _create_min_conv_db(path: Path, with_user_step: bool, user_text: str = "hello") -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)"
        )
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
        if with_user_step:
            user_input = _pb_str(2, user_text)
            payload = _pb_int(1, 14) + _pb_bytes(cp.STEP_USER_INPUT, user_input)
            conn.execute("INSERT INTO steps VALUES (0, 14, 3, NULL, ?)", (payload,))
        else:
            conn.execute("INSERT INTO steps VALUES (0, 15, 3, NULL, ?)", (b"",))
        conn.commit()
    finally:
        conn.close()


def _create_full_conv_db(path: Path, slug: str = "gemini-4-argon-high", budget: int = 256_000) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)"
        )
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")

        step0_ui = _pb_bytes(cp.STEP_USER_INPUT, _pb_str(2, "检查独立上下文解析"))
        step0_payload = _pb_int(1, 14) + _pb_int(4, 3) + step0_ui

        step1_pr = _pb_bytes(cp.STEP_PLANNER_RESPONSE, _pb_str(1, "已确认本地解析器工作正常"))
        step1_md = _pb_bytes(9, _pb_int(3, 1200))
        step1_payload = _pb_int(1, 15) + _pb_int(4, 3) + _pb_bytes(5, step1_md) + step1_pr

        step2_tc = _pb_bytes(4, _pb_str(2, "view_file") + _pb_str(3, '{"Path":"core.py"}'))
        step2_md = _pb_bytes(4, step2_tc) + _pb_int(23, 800)
        step2_payload = _pb_int(1, 8) + _pb_int(4, 3) + _pb_bytes(5, step2_md)

        conn.executemany(
            "INSERT INTO steps VALUES (?, ?, ?, NULL, ?)",
            [
                (0, 14, 3, step0_payload),
                (1, 15, 3, step1_payload),
                (2, 8, 3, step2_payload),
            ],
        )

        # TopLevelTokenGroup breakdown inside gen -> chat(1) -> chat_start(9) -> cwm(10) -> breakdown(3)
        grp_sys = _pb_int(2, cp.TOKEN_SYSTEM_PROMPT) + _pb_int(4, 5_000)
        grp_tools = _pb_int(2, cp.TOKEN_TOOLS) + _pb_int(4, 4_000)
        grp_skills = _pb_int(2, cp.TOKEN_SKILLS) + _pb_int(4, 3_000)
        chat_items = (
            _pb_bytes(5, _pb_str(1, "0: USER_INPUT") + _pb_int(3, 400))
            + _pb_bytes(5, _pb_str(1, "1: PLANNER_RESPONSE") + _pb_int(3, 1_200))
            + _pb_bytes(5, _pb_str(1, "2: VIEW_FILE") + _pb_int(3, 800))
        )
        grp_chat = _pb_int(2, cp.TOKEN_CHAT_MESSAGES) + _pb_int(4, 2_400) + chat_items
        breakdown = _pb_bytes(1, grp_sys) + _pb_bytes(1, grp_tools) + _pb_bytes(1, grp_skills) + _pb_bytes(1, grp_chat)
        cwm = _pb_bytes(3, breakdown) + _pb_int(4, budget)
        chat_start = _pb_bytes(10, cwm)
        chat = _pb_bytes(9, chat_start)
        planner = _pb_str(28, slug)
        gen = _pb_bytes(1, chat) + _pb_bytes(3, planner)

        conn.execute("INSERT INTO gen_metadata VALUES (0, ?)", (gen,))
        conn.commit()
    finally:
        conn.close()


class TestHudCore(unittest.TestCase):
    def test_wal_mtime_prioritizes_active_and_ignores_shm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_old = root / "aaaa1111-0000-0000-0000-000000000001.db"
            shm_old = root / "aaaa1111-0000-0000-0000-000000000001.db-shm"
            db_active = root / "bbbb2222-0000-0000-0000-000000000002.db"
            wal_active = root / "bbbb2222-0000-0000-0000-000000000002.db-wal"

            db_old.write_bytes(b"old")
            shm_old.write_bytes(b"shm")
            db_active.write_bytes(b"active")
            wal_active.write_bytes(b"wal")

            os.utime(db_active, (1000, 1000))
            os.utime(db_old, (2000, 2000))
            os.utime(wal_active, (3000, 3000))
            os.utime(shm_old, (9000, 9000))

            ordered = core.list_conversation_dbs(root)
            self.assertEqual([item[0] for item in ordered], [db_active.stem, db_old.stem])

    def test_skips_subagent_db_without_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_user = root / "11111111-0000-0000-0000-000000000001.db"
            db_sub = root / "22222222-0000-0000-0000-000000000002.db"
            _create_min_conv_db(db_user, with_user_step=True)
            _create_min_conv_db(db_sub, with_user_step=False)

            os.utime(db_user, (1000, 1000))
            os.utime(db_sub, (5000, 5000))

            svc = core.ContextMonitorService(conversations_dir=root)
            target = svc.resolve_target()
            self.assertEqual(target, db_user)

    def test_empty_conversations_dir_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = core.ContextMonitorService(conversations_dir=root)
            self.assertIsNone(service.get_snapshot())
            self.assertEqual(service.list_recent_conversations(), [])

    def test_standalone_protobuf_snapshot_and_native_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "33333333-0000-0000-0000-000000000003.db"
            _create_full_conv_db(db_path, slug="gemini-4-argon-high", budget=256_000)

            service = core.ContextMonitorService(conversations_dir=root)
            snap = service.get_snapshot()
            self.assertIsNotNone(snap)
            assert snap is not None

            self.assertEqual(snap.title, "检查独立上下文解析")
            self.assertEqual(snap.model_id, "gemini-4-argon-high")
            self.assertEqual(snap.limit_tokens, 1_048_576)
            self.assertEqual(snap.fmt_limit, "1.0M")
            self.assertEqual(snap.request_budget_tokens, 256_000)
            self.assertTrue(snap.has_breakdown)
            self.assertEqual(snap.used_tokens, 14_400)
            self.assertEqual(snap.chat_tokens, 1_600)
            self.assertEqual(snap.tool_tokens, 800)
            self.assertEqual(snap.system_total_tokens, 12_000)
            self.assertEqual(
                snap.chat_tokens + snap.tool_tokens + snap.system_total_tokens,
                snap.used_tokens,
            )

    def test_real_conversations_if_present(self) -> None:
        if not core.DEFAULT_CONVERSATIONS_DIR.is_dir():
            self.skipTest("Antigravity conversations dir not present")
        service = core.ContextMonitorService()
        snap = service.get_snapshot()
        if snap is None:
            self.skipTest("No conversation database found")
        self.assertGreater(snap.limit_tokens, 0)
        self.assertGreaterEqual(snap.used_tokens, 0)
        self.assertEqual(
            snap.chat_tokens + snap.tool_tokens + snap.system_total_tokens,
            snap.used_tokens,
        )
        snap2 = service.get_snapshot(snap.conversation_id)
        self.assertEqual(snap.conversation_id, snap2.conversation_id)


if __name__ == "__main__":
    unittest.main()
