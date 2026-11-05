"""Model-facing tools and the lane-file guard."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from .core import Settings
from .store import ACTIVE, DORMANT, STATUSES, LaneError, LaneStore

LANE_LIST = {
    "name": "lane_list",
    "description": (
        "List the user's lanes (projects/ideas) with status, next action and staleness. "
        "Active lanes are current commitments; dormant lanes are saved ideas, not commitments. "
        "The live summary already arrives each turn, so call this only when you need full detail."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(STATUSES) + ["all"],
                       "description": "Filter by status (default: all)."},
        },
    },
}

LANE_CAPTURE = {
    "name": "lane_capture",
    "description": (
        "Save a new project or idea the user mentioned as a DORMANT lane, so it is preserved without "
        "becoming active work. Use this when the user brings up something that does not fit an active lane. "
        "If a similar lane exists you will get it back instead; then use lane_checkpoint on it, "
        "or pass force=true only if it is truly different. This tool cannot make a lane active; "
        "only the user can, with /lanes promote."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short human title, e.g. 'Garage workshop rebuild'."},
            "summary": {"type": "string", "description": "One to three sentences in the user's terms: what it is and why it came up."},
            "force": {"type": "boolean", "description": "Create even if a similar lane exists."},
        },
        "required": ["title", "summary"],
    },
}

LANE_CHECKPOINT = {
    "name": "lane_checkpoint",
    "description": (
        "Record a settled decision, result, blocker or note on a lane, optionally updating its next action. "
        "Keeps the lane's log current so it does not go stale and survives context compression. "
        "Does not change the lane's status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lane": {"type": "string", "description": "Lane slug or title."},
            "kind": {"type": "string", "enum": ["decision", "progress", "blocker", "note"],
                     "description": "What kind of entry this is."},
            "note": {"type": "string", "description": "One line. Concrete: what was decided/done/blocked."},
            "next_action": {"type": "string", "description": "New next action, if it changed."},
        },
        "required": ["lane", "kind", "note"],
    },
}


def make_handlers(get_store: Callable[[], LaneStore]):
    def _ok(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def lane_list(args: dict, **kwargs) -> str:
        try:
            store = get_store()
            status = (args or {}).get("status") or "all"
            lanes = store.all() if status == "all" else store.by_status(status)
            return _ok({"lanes": [ln.to_dict() for ln in lanes], "lanes_dir": str(store.root)})
        except Exception as exc:  # never raise into the agent loop
            return _ok({"error": f"lane_list failed: {exc}"})

    def lane_capture(args: dict, **kwargs) -> str:
        try:
            store = get_store()
            title = str((args or {}).get("title") or "").strip()
            summary = str((args or {}).get("summary") or "").strip()
            if not title:
                return _ok({"error": "title is required"})
            if not args.get("force"):
                dup = store.similar(title)
                if dup:
                    return _ok({
                        "created": False,
                        "similar_existing": [d.to_dict() for d in dup[:3]],
                        "hint": "Add to the existing lane with lane_checkpoint, or retry with force=true if it is genuinely different.",
                    })
            ln = store.create(title, why=summary, status=DORMANT, source="captured by agent")
            return _ok({
                "created": True, "lane": ln.to_dict(),
                "tell_user": f"Saved '{ln.title}' as a dormant lane (not active work). "
                             f"They can promote it with /lanes promote {ln.slug}.",
            })
        except LaneError as exc:
            return _ok({"error": str(exc)})
        except Exception as exc:
            return _ok({"error": f"lane_capture failed: {exc}"})

    def lane_checkpoint(args: dict, **kwargs) -> str:
        try:
            store = get_store()
            ln = store.find(str((args or {}).get("lane") or ""))
            kind = str(args.get("kind") or "note")
            if kind not in {"decision", "progress", "blocker", "note"}:
                kind = "note"
            nxt = args.get("next_action")
            ln = store.append_log(ln, kind, str(args.get("note") or ""),
                                  next_action=str(nxt) if nxt else None)
            out = {"recorded": True, "lane": ln.to_dict()}
            if ln.status != ACTIVE:
                out["note"] = f"'{ln.slug}' is {ln.status}; this did not make it active."
            return _ok(out)
        except LaneError as exc:
            return _ok({"error": str(exc)})
        except Exception as exc:
            return _ok({"error": f"lane_checkpoint failed: {exc}"})

    return lane_list, lane_capture, lane_checkpoint


# ---------------------------------------------------------------------------
# Guard: lane status is the user's call, so the model may not rewrite lane
# files through the generic file or terminal tools. Best-effort: it is a
# policy seam, not a sandbox.
# ---------------------------------------------------------------------------
_WRITE_TOOLS = {"write_file", "patch", "edit_file", "str_replace", "apply_patch"}
_SHELL_TOOLS = {"terminal", "shell", "bash"}
_SHELL_WRITE = re.compile(
    r"(>|\btee\b|\bsed\s+-i|\bperl\s+-[a-z]*i|\brm\b|\bmv\b|\bcp\b|\btouch\b|\btruncate\b|"
    r"\bpython\d*\b|\bnode\b|\bruby\b|\bdd\b|\bln\b|\bchmod\b|\binstall\b)")
# Redirects that write nowhere a lane lives: fd duplication and /dev/null.
_HARMLESS_REDIRECT = re.compile(r"\d*>>?&\d+|&?\d*>>?\s*/dev/null")


def _writes_to(cmd: str, forms: set[str]) -> bool:
    """True when a shell command mentions a lane path AND looks like it writes.

    Splits on command separators so `cat lanes/x.md; mv a b` elsewhere does not
    count, and ignores harmless redirects such as `2>&1` or `>/dev/null`."""
    for part in re.split(r"&&|\|\||;|\n|\|", cmd):
        if not any(f and f in part for f in forms):
            continue
        if _SHELL_WRITE.search(_HARMLESS_REDIRECT.sub(" ", part)):
            return True
    # A redirect into a lane path can sit after a pipe (`echo x | tee lanes/a.md`).
    for f in forms:
        if f and re.search(r"(>>?|\btee\b(\s+-a)?)\s*['\"]?" + re.escape(f), cmd):
            return True
    return False

# The agent must not route around the user's controls through the CLI:
# status-changing `hermes lanes` verbs, Lane Keeper settings, or disabling it.
_SELF_SERVE = re.compile(
    r"\bhermes\b[^\n;|&]*\blanes\s+(promote|park|pause|archive|revive|unarchive|new|add|capture)\b"
    r"|\bhermes\b[^\n;|&]*\bconfig\s+(set|unset|edit)\b[^\n;|&]*lane-keeper"
    r"|\bhermes\b[^\n;|&]*\bplugins\s+(disable|remove|uninstall)\b[^\n;|&]*lane-keeper")

BLOCK_MESSAGE = (
    "Lane files are managed by Lane Keeper. Use lane_capture or lane_checkpoint to record things. "
    "Status changes (promote/park/archive) are the user's call via /lanes."
)


def _cwd() -> Optional[Path]:
    try:
        from agent.runtime_cwd import resolve_agent_cwd  # documented helper
        return Path(resolve_agent_cwd())
    except Exception:
        return None


def _inside(path_text: str, root: Path) -> bool:
    try:
        p = Path(str(path_text)).expanduser()
        if not p.is_absolute():
            base = _cwd()
            if base is None:
                return False
            p = base / p
        p = p.resolve(strict=False)
        r = root.expanduser().resolve(strict=False)
        return p == r or r in p.parents
    except Exception:
        return False


def make_guard(get_settings: Callable[[], Settings]):
    def guard(tool_name: str = "", args: Optional[dict] = None, **kwargs):
        try:
            s = get_settings()
            if not s.guard_lane_files:
                return None
            args = args or {}
            root = s.lanes_dir
            if tool_name in _WRITE_TOOLS:
                for key in ("path", "file_path", "target", "filename"):
                    if args.get(key) and _inside(args[key], root):
                        return {"action": "block", "message": BLOCK_MESSAGE}
                return None
            if tool_name in _SHELL_TOOLS:
                cmd = str(args.get("command") or "")
                if _SELF_SERVE.search(cmd):
                    return {"action": "block", "message": BLOCK_MESSAGE}
                r = root.expanduser().resolve(strict=False)
                forms = {str(root), str(r)}
                home = str(Path.home())
                for f in list(forms):
                    if f.startswith(home):
                        forms.add("~" + f[len(home):])
                        forms.add("$HOME" + f[len(home):])
                if _writes_to(cmd, forms):
                    return {"action": "block", "message": BLOCK_MESSAGE}
            return None
        except Exception:
            return None  # a guard bug must never break unrelated tool calls
    return guard
