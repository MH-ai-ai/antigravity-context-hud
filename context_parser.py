#!/usr/bin/env python3
"""Standalone Antigravity 2.0 conversation database & protobuf context parser.

Decodes the `steps` and `gen_metadata` tables from Antigravity's local SQLite
conversation databases directly with the Python standard library, without
depending on any external plugin or sidecar binary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
import json
import re
import sqlite3

# CortexStep oneof payload field numbers.
STEP_CODE_ACTION = 10
STEP_VIEW_FILE = 14
STEP_USER_INPUT = 19
STEP_PLANNER_RESPONSE = 20
STEP_CHECKPOINT = 30
STEP_GENERIC = 140

GENERIC_TYPE = 132
STATUS_DONE = 3
ORIGINAL_STEP_TYPE_URL = "/gemini_coder.Step"

# TokenType values of TopLevelTokenGroup.type.
TOKEN_SYSTEM_PROMPT = 1
TOKEN_TOOLS = 2
TOKEN_RULES = 3
TOKEN_MCP_TOOLS = 4
TOKEN_SKILLS = 5
TOKEN_SUBAGENTS = 6
TOKEN_CHAT_MESSAGES = 7
TOKEN_WORKFLOWS = 8
TOKEN_PLUGINS = 9
TOKEN_FLOWS = 10

CAT_USER, CAT_AGENT, CAT_TOOL, CAT_CHECKPOINT, CAT_SYSTEM = 0, 1, 2, 3, 4

# CortexStepType names (exa.cortex_pb), used to parse "N: TYPE" chat items.
STEP_TYPES: dict[str, int] = {
    "UNSPECIFIED": 0,
    "DUMMY": 1,
    "FINISH": 2,
    "PLAN_INPUT": 3,
    "MQUERY": 4,
    "CODE_ACTION": 5,
    "GIT_COMMIT": 6,
    "GREP_SEARCH": 7,
    "VIEW_FILE": 8,
    "LIST_DIRECTORY": 9,
    "COMPILE": 10,
    "VIEW_CODE_ITEM": 13,
    "USER_INPUT": 14,
    "PLANNER_RESPONSE": 15,
    "ERROR_MESSAGE": 17,
    "RUN_COMMAND": 21,
    "CHECKPOINT": 23,
    "PROPOSE_CODE": 24,
    "FIND": 25,
    "SUGGESTED_RESPONSES": 27,
    "COMMAND_STATUS": 28,
    "MEMORY": 29,
    "READ_URL_CONTENT": 31,
    "VIEW_CONTENT_CHUNK": 32,
    "SEARCH_WEB": 33,
    "RETRIEVE_MEMORY": 34,
    "MCP_TOOL": 38,
    "MANAGER_FEEDBACK": 39,
    "TOOL_CALL_PROPOSAL": 40,
    "TOOL_CALL_CHOICE": 41,
    "TRAJECTORY_CHOICE": 42,
    "CLIPBOARD": 45,
    "VIEW_FILE_OUTLINE": 47,
    "POST_PR_REVIEW": 49,
    "LIST_RESOURCES": 51,
    "READ_RESOURCE": 52,
    "LINT_DIFF": 53,
    "FIND_ALL_REFERENCES": 54,
    "BRAIN_UPDATE": 55,
    "OPEN_BROWSER_URL": 56,
    "RUN_EXTENSION_CODE": 57,
    "PROPOSAL_FEEDBACK": 59,
    "TRAJECTORY_SEARCH": 60,
    "EXECUTE_BROWSER_JAVASCRIPT": 61,
    "LIST_BROWSER_PAGES": 62,
    "CAPTURE_BROWSER_SCREENSHOT": 63,
    "CLICK_BROWSER_PIXEL": 64,
    "READ_TERMINAL": 65,
    "CAPTURE_BROWSER_CONSOLE_LOGS": 66,
    "READ_BROWSER_PAGE": 67,
    "BROWSER_GET_DOM": 68,
    "CODE_SEARCH": 73,
    "BROWSER_INPUT": 74,
    "BROWSER_MOVE_MOUSE": 75,
    "BROWSER_SELECT_OPTION": 76,
    "BROWSER_SCROLL_UP": 77,
    "BROWSER_SCROLL_DOWN": 78,
    "BROWSER_CLICK_ELEMENT": 79,
    "BROWSER_PRESS_KEY": 80,
    "TASK_BOUNDARY": 81,
    "NOTIFY_USER": 82,
    "CODE_ACKNOWLEDGEMENT": 83,
    "INTERNAL_SEARCH": 84,
    "BROWSER_SUBAGENT": 85,
    "FILE_CHANGE": 86,
    "MOVE": 87,
    "BROWSER_SCROLL": 88,
    "KNOWLEDGE_GENERATION": 89,
    "EPHEMERAL_MESSAGE": 90,
    "GENERATE_IMAGE": 91,
    "DELETE_DIRECTORY": 92,
    "COMPILE_APPLET": 93,
    "INSTALL_APPLET_DEPENDENCIES": 94,
    "INSTALL_APPLET_PACKAGE": 95,
    "BROWSER_RESIZE_WINDOW": 96,
    "BROWSER_DRAG_PIXEL_TO_PIXEL": 97,
    "CONVERSATION_HISTORY": 98,
    "KNOWLEDGE_ARTIFACTS": 99,
    "SEND_COMMAND_INPUT": 100,
    "SYSTEM_MESSAGE": 101,
    "WAIT": 102,
    "AGENCY_TOOL_CALL": 103,
    "CIDER_AGENT_DUMMY": 104,
    "BUILD_CLEANER": 105,
    "BLAZE_BUILD_TARGETS": 106,
    "BLAZE_TEST_TARGETS": 107,
    "SET_UP_FIREBASE": 108,
    "MOMA": 109,
    "RESTART_DEV_SERVER": 110,
    "DEPLOY_FIREBASE": 111,
    "SHELL_EXEC": 112,
    "BROWSER_MOUSE_WHEEL": 113,
    "LINT_APPLET": 114,
    "KI_INSERTION": 116,
    "RETRIEVE_CONTENT": 117,
    "CRITIQUE": 118,
    "FINDINGS": 119,
    "BROWSER_MOUSE_UP": 120,
    "BROWSER_MOUSE_DOWN": 121,
    "WORKSPACE_API": 122,
    "BROWSER_LIST_NETWORK_REQUESTS": 123,
    "BROWSER_GET_NETWORK_REQUEST": 124,
    "BROWSER_REFRESH_PAGE": 125,
    "EDIT_NOTEBOOK": 126,
    "INVOKE_SUBAGENT": 127,
    "WRITE_BLOB": 128,
    "READ_NOTEBOOK": 129,
    "PROPOSE_AI_COMMENTS": 130,
    "START_CODE_REVIEW": 131,
    "GENERIC": 132,
    "SET_UP_CLOUD_SQL": 133,
    "EXECUTE_NOTEBOOK": 134,
    "CLOUD_SQL_UPDATE_SCHEMA": 135,
    "RPC_ACTION": 136,
    "CLOUD_SQL_EXECUTE_SQL": 137,
    "ASK_QUESTION": 138,
    "DIRECTORY_RULES": 139,
    "TOOL_SEARCH": 140,
}

CATEGORIES: list[tuple[str, str, str, bool]] = [
    ("◉", "User messages", "user", True),
    ("◉", "Agent responses", "agent", True),
    ("◉", "Tool calls", "tool", True),
    ("⛁", "System prompt", "system_prompt", False),
    ("⛁", "System tools", "system_tools", False),
    ("⛁", "System overhead", "system_overhead", False),
    ("⛁", "Rules", "rules", False),
    ("⛁", "Skills", "skills", False),
    ("⛁", "Subagents", "subagents", False),
    ("⛁", "Legacy workflows", "legacy_workflows", False),
    ("⛁", "Workflows", "workflows", False),
    ("⛁", "Plugins", "plugins", False),
    ("⛁", "MCP tools", "mcp_tools", False),
    ("◉", "System messages", "system_messages", False),
]

# Built-in Antigravity model catalog so native context limits never depend on
# external plugins or runtime sidecar caches.
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "chat_20706": {"maxTokens": 16384, "displayName": ""},
    "chat_23310": {"maxTokens": 32768, "displayName": ""},
    "claude-opus-5-5-high": {"maxTokens": 1_000_000, "displayName": "Claude Opus 5.5 (High)"},
    "claude-opus-5-5-low": {"maxTokens": 1_000_000, "displayName": "Claude Opus 5.5 (Low)"},
    "claude-opus-5-5-medium": {"maxTokens": 1_000_000, "displayName": "Claude Opus 5.5 (Medium)"},
    "claude-sonnet-5-5-high": {"maxTokens": 1_000_000, "displayName": "Claude Sonnet 5.5 (High)"},
    "claude-sonnet-5-5-low": {"maxTokens": 1_000_000, "displayName": "Claude Sonnet 5.5 (Low)"},
    "claude-sonnet-5-5-medium": {"maxTokens": 1_000_000, "displayName": "Claude Sonnet 5.5 (Medium)"},
    "gemini-2.5-flash": {"maxTokens": 1_048_576, "displayName": "Gemini 3.5 Flash Lite"},
    "gemini-2.5-flash-lite": {"maxTokens": 1_048_576, "displayName": "Gemini 3.5 Flash Lite"},
    "gemini-2.5-flash-thinking": {"maxTokens": 1_048_576, "displayName": "Gemini 3.5 Flash Lite"},
    "gemini-2.5-pro": {"maxTokens": 1_048_576, "displayName": "Gemini 2.5 Pro"},
    "gemini-3-flash": {"maxTokens": 1_048_576, "displayName": "Gemini 3 Flash"},
    "gemini-3.1-flash-image": {"maxTokens": 65_536, "displayName": "Gemini 3.1 Flash Image"},
    "gemini-3.1-flash-lite": {"maxTokens": 1_048_576, "displayName": "Gemini 3.1 Flash Lite"},
    "gemini-3.5-flash-lite": {"maxTokens": 1_048_576, "displayName": "Gemini 3.5 Flash Lite"},
    "gemini-3.8-flash-high": {"maxTokens": 1_048_576, "displayName": "Gemini 3.8 Flash (High)"},
    "gemini-3.8-flash-low": {"maxTokens": 1_048_576, "displayName": "Gemini 3.8 Flash (Low)"},
    "gemini-3.8-flash-medium": {"maxTokens": 1_048_576, "displayName": "Gemini 3.8 Flash (Medium)"},
    "gemini-3.8-flash-tiered": {"maxTokens": 1_048_576, "displayName": ""},
    "gemini-4-argon-high": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Argon (High)"},
    "gemini-4-argon-low": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Argon (Low)"},
    "gemini-4-argon-medium": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Argon (Medium)"},
    "gemini-4-carbon-disabled": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Carbon"},
    "gemini-4-hydrogen-high": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Hydrogen (High)"},
    "gemini-4-hydrogen-low": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Hydrogen (Low)"},
    "gemini-4-hydrogen-medium": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Hydrogen (Medium)"},
    "gemini-4-hydrogen-xhigh": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Hydrogen (XHigh)"},
    "gemini-flash-worker-agy": {"maxTokens": 1_048_576, "displayName": "Gemini Flash"},
    "gemini-pro-orchestrator-g1-pro": {"maxTokens": 1_048_576, "displayName": "Orchestrator (Argon)"},
    "gpt-oss-120b-medium": {"maxTokens": 131_072, "displayName": "GPT-OSS 120B (Medium)"},
    "tab_flash_lite_preview": {"maxTokens": 16_384, "displayName": ""},
    "tab_jump_flash_lite_preview": {"maxTokens": 16_384, "displayName": ""},
}

# Prefix fallbacks when a newer model slug is not yet listed in MODEL_CATALOG.
NATIVE_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("claude-opus-4-6", 250_000),
    ("claude-sonnet-4-6", 250_000),
    ("claude-", 1_000_000),
    ("gpt-oss-", 131_072),
    ("gemini-", 1_048_576),
]

EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
WORD_LABELS = {"gpt": "GPT", "oss": "OSS"}


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("Truncated varint")


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Msg:
    """Lazily decoded protobuf message: field number -> list of raw values."""

    def __init__(self, buf: bytes | None):
        self.fields: dict[int, list[Any]] = {}
        raw = buf or b""
        pos, total_len = 0, len(raw)
        while pos < total_len:
            try:
                tag, pos = read_varint(raw, pos)
                fn, wt = tag >> 3, tag & 7
                if wt == 0:
                    val, pos = read_varint(raw, pos)
                elif wt == 2:
                    ln, pos = read_varint(raw, pos)
                    val = raw[pos : pos + ln]
                    pos += ln
                elif wt == 1:
                    val, pos = raw[pos : pos + 8], pos + 8
                elif wt == 5:
                    val, pos = raw[pos : pos + 4], pos + 4
                else:
                    break
            except ValueError:
                break
            self.fields.setdefault(fn, []).append(val)

    def has(self, fn: int) -> bool:
        return fn in self.fields

    def int(self, fn: int, signed32: bool = False) -> int:
        v = self.fields.get(fn, [0])[-1]
        v = v if isinstance(v, int) else 0
        if signed32 and v >= 1 << 63:
            v -= 1 << 64
        return v

    def bytes(self, fn: int) -> bytes:
        v = self.fields.get(fn, [b""])[-1]
        return v if isinstance(v, bytes) else b""

    def str(self, fn: int) -> str:
        return self.bytes(fn).decode("utf-8", errors="replace")

    def msg(self, fn: int) -> Msg | None:
        return Msg(self.bytes(fn)) if self.has(fn) else None

    def msgs(self, fn: int) -> list[Msg]:
        return [Msg(v) for v in self.fields.get(fn, []) if isinstance(v, bytes)]


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000.0
        return f"{v:.0f}M" if v.is_integer() else f"{v:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000.0:.1f}k"
    return str(n)


def fmt_ctx_limit(n: int) -> str:
    return fmt_tok(n)


def go_div4(n: int) -> int:
    """Truncating integer division by 4 matching Go's token heuristic."""
    return int(n / 4)


def shorten_path(uri: str) -> str:
    p = uri
    if p.startswith("file:"):
        parsed = urlparse(p)
        p = unquote(parsed.path)
        if len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
    home = str(Path.home()).replace("\\", "/")
    p = p.replace("\\", "/")
    if p.startswith(home):
        return "~" + p[len(home) :]
    return p


def step_category(step_type: int) -> int:
    if step_type == 14:
        return CAT_USER
    if step_type == 15:
        return CAT_AGENT
    if step_type == 23:
        return CAT_CHECKPOINT
    if step_type in (0, 2, 101):
        return CAT_SYSTEM
    return CAT_TOOL


def packed_original_step(generic: Msg) -> bytes | None:
    """Extract the wrapped typed step payload from a GENERIC step."""
    result = generic.msg(2)
    if result is None:
        return None
    payload = result.msg(6)
    if payload is not None and payload.str(1).endswith(ORIGINAL_STEP_TYPE_URL):
        return payload.bytes(2)
    for entry in result.msgs(5):
        if entry.str(1) == "__original_cortex_step":
            value = entry.msg(2)
            if value is not None and value.str(1).endswith(ORIGINAL_STEP_TYPE_URL):
                return value.bytes(2)
    return None


def is_skill_file(path: str) -> bool:
    return path.replace("\\", "/").endswith("/SKILL.md")


class Step:
    def __init__(self, pos: int, payload: bytes, step_type: int, status: int, metadata: bytes | None):
        self.pos = pos
        self.pb = Msg(payload)
        self.type = self.pb.int(1) if self.pb.has(1) else step_type
        self.status = self.pb.int(4) if self.pb.has(4) else status
        self.md = self.pb.msg(5) or Msg(metadata)
        self.view_file_override: Msg | None = None
        if self.type == GENERIC_TYPE:
            self.rehydrate()

    def rehydrate(self) -> None:
        """Restore a GENERIC step back into its typed VIEW_FILE or original step."""
        generic = self.pb.msg(STEP_GENERIC)
        if generic is None:
            return
        original = packed_original_step(generic)
        if original is not None:
            self.pb = Msg(original)
            self.type = self.pb.int(1)
            self.md = self.pb.msg(5) or Msg(b"")
            return
        tc = self.md.msg(4)
        if tc is None or tc.str(2) not in ("view_file", "read_file"):
            return
        try:
            args = json.loads(tc.str(3) or "{}")
        except json.JSONDecodeError:
            return
        path = next(
            (args[k] for k in ("AbsolutePath", "Path", "FilePath") if isinstance(args.get(k), str) and args[k]),
            "",
        )
        if path:
            self.type = 8  # VIEW_FILE
            encoded = path.encode("utf-8")
            self.view_file_override = Msg(
                b"\x0a" + _encode_varint(len(encoded)) + encoded + (b"\x88\x01\x01" if is_skill_file(path) else b"")
            )

    def payload(self, fn: int) -> Msg | None:
        if fn == STEP_VIEW_FILE and self.view_file_override is not None:
            return self.view_file_override
        return self.pb.msg(fn)


def estimate_step_tokens(step: Step) -> int:
    ui = step.payload(STEP_USER_INPUT)
    pr = step.payload(STEP_PLANNER_RESPONSE)
    if ui is not None:
        n = len(ui.bytes(2))
    elif pr is not None:
        n = len(pr.bytes(1))
    else:
        tc = step.md.msg(4)
        n = len(tc.bytes(3)) if tc else 0
        out = step.md.int(23, signed32=True)
        if out > 0:
            return out
    if n == 0:
        return 0
    return max(1, go_div4(n))


def step_tokens(step: Step) -> int:
    usage = step.md.msg(9)
    if usage is not None:
        return usage.int(3)
    return estimate_step_tokens(step)


def compaction_tokens(step: Step) -> int:
    for ri in reversed(step.md.msgs(28)):
        usage = ri.msg(2)
        if usage is None:
            continue
        resp = usage.int(10)
        if resp:
            return resp
        out, thinking = usage.int(3), usage.int(9)
        return out - thinking if thinking < out else 0
    return 0


def checkpoint_summary_tokens(step: Step) -> int:
    t = compaction_tokens(step)
    if t:
        return t
    ck = step.payload(STEP_CHECKPOINT) or Msg(b"")
    return go_div4(len(ck.bytes(4)) + len(ck.bytes(5)) + len(ck.bytes(6)))


def checkpoint_newer_than_generation(steps: list[Step], chat_start: Msg | None) -> tuple[int, Step | None]:
    i = len(steps) - 1
    while i >= 0:
        s = steps[i]
        if s.payload(STEP_CHECKPOINT) is not None and s.status == STATUS_DONE:
            break
        i -= 1
    if i < 0:
        return -1, None
    for s in steps[i + 1 :]:
        if s.payload(STEP_PLANNER_RESPONSE) is not None and s.status == STATUS_DONE:
            return -1, None
    gen_ck = chat_start.int(2, signed32=True) if chat_start is not None else -1
    ck_idx = steps[i].payload(STEP_CHECKPOINT).int(1)
    if gen_ck >= ck_idx:
        return -1, None
    return i, steps[i]


def chat_tokens_by_category(items: list[Msg]) -> tuple[bool, list[int]]:
    out = [0, 0, 0, 0]  # user, agent, tool, system
    if not items:
        return False, out
    for it in items:
        left, sep, right = it.str(1).partition(": ")
        if not sep or not re.fullmatch(r"[+-]?[0-9]+", left) or right not in STEP_TYPES:
            return False, [0, 0, 0, 0]
        cat = step_category(STEP_TYPES[right])
        tok = it.int(3, signed32=True)
        if cat == CAT_USER:
            out[0] += tok
        elif cat in (CAT_AGENT, CAT_CHECKPOINT):
            out[1] += tok
        elif cat == CAT_TOOL:
            out[2] += tok
        else:
            out[3] += tok
    return True, out


def detect_system_files(home: str, workspace: str | None, app_dir: str) -> list[tuple[str, int]]:
    if not home:
        return []
    gemini = Path(home) / ".gemini"
    disp = "~/" + gemini.name
    found: list[str] = []
    f = gemini / "GEMINI.md"
    if f.exists() and not f.is_dir():
        found.append(disp + "/GEMINI.md")
    for sub in ("skills", "rules", "agents", "global_workflows"):
        if (gemini / sub).is_dir():
            found.append(f"{disp}/{sub}/")
    if app_dir:
        for sub in ("rules", "skills", "agents", "global_workflows"):
            if (gemini / app_dir / sub).is_dir():
                found.append(f"{disp}/{app_dir}/{sub}/")
    if workspace:
        for root in (".agents", "_agents"):
            for sub in ("rules", "skills", "agents", "workflows"):
                if (Path(workspace) / root / sub).is_dir():
                    found.append(f"{root}/{sub}/")
            hooks = Path(workspace) / root / "hooks.json"
            if hooks.exists() and not hooks.is_dir():
                found.append(f"{root}/hooks.json")
    return [(p, 0) for p in found]


def collect_stats(steps: list[Step], gen: Msg | None, limit: int) -> dict[str, Any]:
    st: dict[str, Any] = {
        k: 0
        for k in (
            "user",
            "agent",
            "tool",
            "checkpoint",
            "system_messages",
            "system_prompt",
            "system_tools",
            "system_overhead",
            "rules",
            "skills",
            "subagents",
            "legacy_workflows",
            "workflows",
            "plugins",
            "mcp_tools",
        )
    }
    st["has_breakdown"] = False
    st["active_checkpoint"] = -1
    checkpoints: list[dict[str, Any]] = []
    knowledge: list[tuple[str, int]] = []
    artifacts: list[tuple[str, int]] = []
    skill_files: list[tuple[str, int]] = []
    seen_k, seen_a, seen_s = set(), set(), set()

    chat = gen.msg(1) if gen is not None else None
    chat_start = chat.msg(9) if chat is not None else None
    newer_pos, newer_step = checkpoint_newer_than_generation(steps, chat_start)
    has_newer = newer_step is not None
    st["compacted"] = has_newer

    for pos, step in enumerate(steps):
        cat = step_category(step.type)
        tok = step_tokens(step)
        counted = 0 if has_newer and newer_pos > pos else tok
        if cat == CAT_USER:
            st["user"] += counted
        elif cat == CAT_AGENT:
            st["agent"] += counted
        elif cat == CAT_TOOL:
            st["tool"] += counted
        elif cat == CAT_CHECKPOINT:
            st["checkpoint"] += tok
            ck = step.payload(STEP_CHECKPOINT)
            if ck is not None:
                checkpoints.append(
                    {
                        "index": ck.int(1),
                        "start": ck.int(11),
                        "end": ck.int(12),
                        "intent": ck.str(4),
                    }
                )
        else:
            st["system_messages"] += counted

        ca = step.payload(STEP_CODE_ACTION)
        result = ca.msg(2) if ca is not None else None
        edit = result.msg(1) if result is not None else None
        if edit is not None:
            uri = edit.str(8)
            short = shorten_path(uri)
            if ca.int(21) and uri not in seen_a:
                seen_a.add(uri)
                artifacts.append((short, tok))
            if ca.int(24) and uri not in seen_k:
                seen_k.add(uri)
                knowledge.append((short, tok))

        vf = step.payload(STEP_VIEW_FILE)
        if vf is not None and vf.int(17):
            uri = vf.str(1)
            if uri not in seen_s:
                seen_s.add(uri)
                skill_files.append((shorten_path(uri), tok))

    cwm = chat_start.msg(10) if chat_start is not None else None
    breakdown = cwm.msg(3) if cwm is not None else None
    if breakdown is not None:
        st["has_breakdown"] = True
        seen_prompt = seen_tools = False
        chat_tok, chat_items = 0, []
        for g in breakdown.msgs(1):
            gtype, gtok = g.int(2), g.int(4, signed32=True)
            if gtype == TOKEN_SYSTEM_PROMPT:
                extracted = 0
                for child in g.msgs(5):
                    name, ctok = child.str(1), child.int(3, signed32=True)
                    if name == "flows":
                        st["workflows"] += ctok
                    elif name == "skills":
                        st["skills"] += ctok
                    elif name == "subagents":
                        st["subagents"] += ctok
                    else:
                        continue
                    extracted += ctok
                st["system_prompt"] = gtok - extracted
                seen_prompt = True
            elif gtype == TOKEN_TOOLS:
                st["system_tools"] = gtok
                seen_tools = True
            elif gtype == TOKEN_RULES:
                st["rules"] += gtok
            elif gtype == TOKEN_MCP_TOOLS:
                st["mcp_tools"] += gtok
            elif gtype == TOKEN_SKILLS:
                st["skills"] += gtok
            elif gtype == TOKEN_SUBAGENTS:
                st["subagents"] += gtok
            elif gtype == TOKEN_CHAT_MESSAGES:
                chat_tok, chat_items = gtok, g.msgs(5)
            elif gtype == TOKEN_WORKFLOWS:
                st["legacy_workflows"] += gtok
            elif gtype == TOKEN_PLUGINS:
                st["plugins"] += gtok
            elif gtype == TOKEN_FLOWS:
                st["workflows"] += gtok

        if not seen_prompt and not seen_tools and chat_tok:
            usage = chat.msg(4)
            api_input = usage.int(2) + usage.int(5) if usage is not None else 0
            if chat_tok < api_input:
                st["system_overhead"] = api_input - chat_tok

        if chat_tok and not has_newer:
            ok, cats = chat_tokens_by_category(chat_items)
            if ok:
                st["user"], st["agent"], st["tool"], st["system_messages"] = cats
            else:
                est = st["user"] + st["agent"] + st["tool"]
                if est == 0:
                    st["agent"] = chat_tok
                else:
                    f = chat_tok / est
                    st["user"] = int(st["user"] * f)
                    st["agent"] = int(st["agent"] * f)
                    st["tool"] = int(st["tool"] * f)

    if chat_start is not None:
        st["active_checkpoint"] = chat_start.int(2, signed32=True)
    if has_newer:
        st["agent"] += checkpoint_summary_tokens(newer_step)
        st["active_checkpoint"] = newer_step.payload(STEP_CHECKPOINT).int(1)

    st["total"] = sum(
        st[k]
        for k in (
            "user",
            "agent",
            "tool",
            "system_messages",
            "system_prompt",
            "system_tools",
            "system_overhead",
            "rules",
            "skills",
            "subagents",
            "legacy_workflows",
            "workflows",
            "plugins",
            "mcp_tools",
        )
    )
    st["limit"] = limit
    st["checkpoints"] = checkpoints
    st["knowledge_files"] = knowledge
    st["artifact_files"] = artifacts
    st["skill_files"] = skill_files
    return st


def pct(n: int, limit: int) -> float:
    return n / limit * 100.0 if limit else 0.0


def stats_heading(st: dict[str, Any]) -> str:
    if st["compacted"]:
        return "Estimated after compaction · updates next turn"
    if st["has_breakdown"]:
        return "Token usage by category"
    return "Estimated usage (awaiting generation)"


def grid_segments(st: dict[str, Any]) -> list[int]:
    """Token totals of the ring's five colored segments in drawing order."""
    return [
        st["system_prompt"] + st["system_tools"] + st["system_overhead"],
        st["user"],
        st["agent"],
        st["tool"],
        sum(st[k] for k in ("rules", "skills", "subagents", "legacy_workflows", "workflows", "plugins", "mcp_tools")),
    ]


def checkpoint_view(st: dict[str, Any]) -> dict[str, Any] | None:
    cks = st["checkpoints"]
    if not cks:
        return None
    active, historical = None, 0
    for ck in cks:
        if st["active_checkpoint"] >= 0 and ck["index"] == st["active_checkpoint"]:
            active = ck
        else:
            historical += 1
    if active is None:
        active, historical = cks[-1], len(cks) - 1
    text = active["intent"]
    if len(text.encode("utf-8")) > 70:
        text = text.encode("utf-8")[:67].decode("utf-8", errors="ignore") + "..."
    if not text:
        text = f"steps {active['start']}–{active['end']}"
    return {
        "count": len(cks),
        "active_number": active["index"] + 1,
        "active_text": text,
        "historical": historical,
    }


def load(db_path: Path) -> tuple[list[Step], Msg | None]:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        rows = conn.execute(
            "SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx"
        ).fetchall()
        steps = [Step(pos, r[4] or b"", r[1], r[2], r[3]) for pos, r in enumerate(rows)]
        row = conn.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return steps, (Msg(row[0]) if row else None)


def model_id(gen: Msg | None) -> str:
    planner = gen.msg(3) if gen is not None else None
    return planner.str(28) if planner is not None else ""


def model_label(slug: str) -> str:
    """Format model ID as readable display label."""
    parts = slug.split("-")
    effort = parts.pop().capitalize() if len(parts) > 1 and parts[-1] in EFFORT_LEVELS else ""
    words: list[str] = []
    for p in parts:
        if p.isdigit() and words and words[-1][-1:].isdigit():
            words[-1] += "." + p
        elif p in WORD_LABELS:
            words.append(WORD_LABELS[p])
        elif p[:1].isdigit():
            words.append(p.upper())
        else:
            words.append(p.capitalize())
    label = " ".join(words).replace("GPT OSS", "GPT-OSS")
    return f"{label} ({effort})" if effort else label


def lookup_model_catalog(slug: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return built-in catalog metadata merged with any optional override dict."""
    base = dict(MODEL_CATALOG.get(slug) or {})
    if overrides and isinstance(overrides.get(slug), dict):
        base.update({k: v for k, v in overrides[slug].items() if v is not None})
    return base


def model_name(gen: Msg | None) -> str:
    chat = gen.msg(1) if gen is not None else None
    if chat is not None and chat.str(21):
        return chat.str(21)
    slug = model_id(gen)
    if slug:
        return str((MODEL_CATALOG.get(slug) or {}).get("displayName") or model_label(slug))
    if chat is not None and chat.str(19):
        return chat.str(19)
    return "Unknown Model"


def request_budget(gen: Msg | None) -> int:
    chat = gen.msg(1) if gen is not None else None
    start = chat.msg(9) if chat is not None else None
    cwm = start.msg(10) if start is not None else None
    return cwm.int(4, signed32=True) if cwm is not None else 0


def context_limit(gen: Msg | None, catalog_entry: dict[str, Any] | None = None) -> int:
    """Resolve model native window: explicit entry > built-in catalog > prefix > budget."""
    if catalog_entry and int(catalog_entry.get("maxTokens") or 0) > 0:
        return int(catalog_entry["maxTokens"])
    slug = model_id(gen)
    builtin = MODEL_CATALOG.get(slug)
    if builtin and int(builtin.get("maxTokens") or 0) > 0:
        return int(builtin["maxTokens"])
    for prefix, tokens in NATIVE_CONTEXT_WINDOWS:
        if slug.startswith(prefix):
            return tokens
    return request_budget(gen)
