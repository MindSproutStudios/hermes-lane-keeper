"""Lane Keeper core: settings, what the model sees, and the /lanes command.

What the model sees, cache-safe:
- system_section(): rules + a lane snapshot, rendered ONCE per session through
  register_system_prompt_section, then frozen (the prompt cache stays valid).
- turn_delta(): a one-line pre_llm_call note, only when lanes changed since the
  snapshot (a promotion, a capture, a park). None otherwise.

The /lanes slash command is the only place lane *status* changes. The
`hermes lanes` CLI is read-only, so an agent with a terminal can't promote.
"""
from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .store import ACTIVE, ARCHIVED, DORMANT, Lane, LaneError, LaneStore, now

DEFAULTS = {
    "lanes_dir": "",
    "max_active": 3,
    "stale_days": 7,
    "inject": "changes",         # changes | every_turn | off
    "dormant_in_context": 15,
    "guard_lane_files": True,
}
SNAPSHOT_MAX = 2000
UNATTENDED_PLATFORMS = {"cron", "batch", "webhook"}


@dataclass
class Settings:
    lanes_dir: Path
    max_active: int
    stale_days: int
    inject: str
    dormant_in_context: int
    guard_lane_files: bool

    @classmethod
    def load(cls, get: Callable[[str, object], object], default_dir: Callable[[], Path]) -> "Settings":
        def _int(key, lo, hi):
            try:
                return max(lo, min(hi, int(get(key, DEFAULTS[key]))))
            except (TypeError, ValueError):
                return DEFAULTS[key]
        raw_dir = str(get("lanes_dir", "") or "").strip()
        inject = str(get("inject", DEFAULTS["inject"]) or "changes").strip().lower()
        if inject == "first_turn":
            inject = "changes"
        if inject not in {"changes", "every_turn", "off"}:
            inject = "changes"
        guard = get("guard_lane_files", True)
        return cls(
            lanes_dir=Path(raw_dir).expanduser() if raw_dir else default_dir(),
            max_active=_int("max_active", 1, 20),
            stale_days=_int("stale_days", 1, 365),
            inject=inject,
            dormant_in_context=_int("dormant_in_context", 0, 100),
            guard_lane_files=guard if isinstance(guard, bool) else str(guard).lower() not in {"false", "0", "no", "off"},
        )


RULES = """\
Lane Keeper is on. The user keeps a few ACTIVE lanes (current commitments) and a vault of DORMANT lanes (saved ideas that are not commitments). The snapshot below was taken when this session started; if lanes change mid-session, a one-line [Lane Keeper] update arrives with the user's message.
- Treat only active lanes as current work. Never treat a dormant or archived lane as a commitment or nag about it.
- When the user brings up a new project or idea that does not fit an active lane, save it with lane_capture (it lands dormant) and confirm in one short line. If a close lane already exists, add to it with lane_checkpoint instead of duplicating it. Ordinary requests, questions and tasks inside an existing lane are not new ideas: do not capture them.
- Do not quietly turn a new idea into active work. If the user explicitly asks you to work on it now, do the work, but say it is outside the active lanes.
- Only the user changes lane status, by typing /lanes promote, /lanes park or /lanes archive in chat. You have no tool for that, must not edit lane files or Lane Keeper's data, and must not change its settings. Never offer to promote a lane yourself; point the user to the command. When promotion is at the active-lane cap, name which active lane would have to park.
- When a decision, blocker, result or next action is settled on a lane, record it with lane_checkpoint (one line; include next_action when it changes).
- If the snapshot says to mention a stale lane or a status mismatch, do it once, briefly, at a natural point."""


def _ago(days: Optional[int]) -> str:
    if days is None:
        return "never updated"
    if days == 0:
        return "today"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


def fingerprint(store: LaneStore) -> str:
    parts = sorted(f"{ln.slug}|{ln.status}|{ln.next_action}|{ln.mismatch}" for ln in store.all())
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _active_line(ln: Lane, s: Settings, ref, stale_note: str = "") -> str:
    days = ln.age_days(ref)
    nxt = ln.next_action or "(none set)"
    return f"- {ln.title} [`{ln.slug}`]{stale_note} · updated {_ago(days)} | next: {nxt}"


def snapshot(store: LaneStore, s: Settings, *, mark_stale: bool = True) -> str:
    """Lane list for the model (≤ SNAPSHOT_MAX chars). Gives each stale notice once."""
    lanes = store.all()
    if not lanes:
        return ("[Lane Keeper] No lanes yet. When the user names projects or ideas, capture them "
                "with lane_capture; they can make one active with /lanes promote <name>.")
    ref = now()
    active = store.by_status(ACTIVE)
    dormant = store.by_status(DORMANT)
    reg = store.registry
    notified = reg.data["stale_notified"] if reg is not None else {}
    lines = [f"[Lane Keeper] Active lanes ({len(active)}/{s.max_active}):"]
    if not active:
        lines.append("- none: nothing is an active commitment right now")
    changed = False
    for ln in active:
        days = ln.age_days(ref)
        note = ""
        if days is not None and days >= s.stale_days:
            stamp = ln.updated.isoformat() if ln.updated else "never"
            if notified.get(ln.slug) != stamp:
                note = f" ⚠ STALE {days}d (mention once)"
                if mark_stale:
                    notified[ln.slug] = stamp
                    changed = True
            else:
                note = f" ⚠ stale {days}d (already mentioned)"
        lines.append(_active_line(ln, s, ref, note))
    if len(active) > s.max_active:
        lines.append(f"OVER CAP: {len(active)} active vs a cap of {s.max_active}. A focus problem only the "
                     "user can resolve with /lanes park. Do not pick for them.")
    bad = [ln for ln in lanes if ln.mismatch]
    if bad:
        desc = "; ".join(f"`{ln.slug}` (file says {ln.file_status}, really {ln.status})" for ln in bad[:5])
        lines.append(f"STATUS MISMATCH (mention once): lane files claim a status the user never set: {desc}. "
                     "The real status stands. If the user meant it, they run /lanes promote.")
    if dormant and s.dormant_in_context:
        shown = dormant[: s.dormant_in_context]
        names = "; ".join(f"{ln.title} [`{ln.slug}`]" for ln in shown)
        more = f" (+{len(dormant) - len(shown)} more)" if len(dormant) > len(shown) else ""
        lines.append(f"Dormant, saved but NOT commitments ({len(dormant)}): {names}{more}")
    elif dormant:
        lines.append(f"Dormant lanes saved: {len(dormant)} (not commitments).")
    if changed and reg is not None:
        reg.save()
    text = "\n".join(lines)
    if len(text) > SNAPSHOT_MAX:
        text = text[: SNAPSHOT_MAX - 40].rsplit("\n", 1)[0] + "\n(…more lanes: use lane_list)"
    return text


def _remember_session(store: LaneStore, session_id: str) -> None:
    reg = store.registry
    if reg is None or not session_id:
        return
    sessions = reg.data["sessions"]
    sessions[session_id] = fingerprint(store)
    if len(sessions) > 300:  # bounded: drop the oldest entries
        for key in list(sessions)[: len(sessions) - 300]:
            sessions.pop(key, None)
    reg.save()


def system_section(store: LaneStore, s: Settings, session_id: str = "") -> str:
    text = RULES + "\n\n" + snapshot(store, s)
    _remember_session(store, session_id)
    return text


def turn_delta(store: LaneStore, s: Settings, session_id: str) -> Optional[str]:
    """One short update when lanes changed since this session's snapshot, else None."""
    reg = store.registry
    if reg is None or not session_id:
        return None
    fp = fingerprint(store)
    prev = reg.data["sessions"].get(session_id)
    if prev == fp:
        return None
    reg.data["sessions"][session_id] = fp
    reg.save()
    if prev is None:
        return None  # session predates the plugin or its record; nothing to compare
    active = store.by_status(ACTIVE)
    act = "; ".join(f"{ln.title} [`{ln.slug}`] next: {ln.next_action or '(none)'}" for ln in active) or "none"
    dormant = len(store.by_status(DORMANT))
    note = f"[Lane Keeper] Lanes changed this session. Active now ({len(active)}/{s.max_active}): {act}. Dormant: {dormant}."
    bad = [ln.slug for ln in store.all() if ln.mismatch]
    if bad:
        note += f" Status mismatch on: {', '.join(bad[:5])} (the real status stands)."
    return note[:SNAPSHOT_MAX]


def context_block(store: LaneStore, s: Settings, *, first_turn: bool) -> Optional[str]:
    """Fallback for hosts without system-prompt sections, or inject=every_turn."""
    if not store.all() and not first_turn:
        return None
    return snapshot(store, s, mark_stale=first_turn)


# ---------------------------------------------------------------------------
# /lanes command: the chat slash command can do everything; `hermes lanes`
# in a terminal is read-only (see run_command(read_only=True)).
# ---------------------------------------------------------------------------
HELP = """\
/lanes                         brief: active lanes, next actions, stale flags
/lanes all | dormant | archived  list lanes
/lanes show <lane>             one lane with its recent log
/lanes new <title> [-- why]    save a new dormant lane
/lanes promote <lane> [--park <lane>]   make a lane active (cap-aware)
/lanes park <lane> [-- reason]          active → dormant
/lanes archive <lane> [-- reason]       → archived (kept, out of view)
/lanes next <lane> -- <next action>     set the next action
/lanes log <lane> -- <note>             add a log line"""


def _split_tail(args: list[str]) -> tuple[list[str], str]:
    """Split `a b -- free text` into (["a","b"], "free text")."""
    if "--" in args:
        i = args.index("--")
        return args[:i], " ".join(args[i + 1:]).strip()
    return args, ""


def _line(ln: Lane, ref, stale_days: int) -> str:
    days = ln.age_days(ref)
    stale = " ⚠ stale" if ln.status == ACTIVE and days is not None and days >= stale_days else ""
    nxt = f"\n    next: {ln.next_action}" if ln.next_action else ""
    return f"• {ln.title} (`{ln.slug}`){stale} · {_ago(days)}{nxt}"


def brief(store: LaneStore, s: Settings) -> str:
    ref = now()
    active = store.by_status(ACTIVE)
    dormant = store.by_status(DORMANT)
    archived = store.by_status(ARCHIVED)
    if not (active or dormant or archived):
        return ("No lanes yet. Save one with `/lanes new <title>`, or just tell the agent about an idea. "
                f"Lanes live in {s.lanes_dir}")
    out = [f"🛣 Active lanes {len(active)}/{s.max_active}"]
    out += [_line(ln, ref, s.stale_days) for ln in active] or ["• none"]
    if len(active) > s.max_active:
        out.append(f"\n🚨 Over cap: {len(active)} active, cap is {s.max_active}. "
                   f"Park {len(active) - s.max_active}: /lanes park <lane>")
    stale = [ln for ln in active if (ln.age_days(ref) or 0) >= s.stale_days]
    if stale:
        out.append(f"\n⚠ {len(stale)} stale (no update in {s.stale_days}+ days): "
                   + ", ".join(ln.slug for ln in stale) + ". Push it forward, or park it.")
    bad = [ln for ln in store.all() if ln.mismatch]
    if bad:
        out.append("\n🔒 Ignored status edits: these files claim a status you never set, so the real status stands: "
                   + ", ".join(f"`{ln.slug}` (file: {ln.file_status}, real: {ln.status})" for ln in bad[:5])
                   + ". If you meant it, use /lanes promote/park.")
    if dormant:
        recent = dormant[:5]
        out.append(f"\n💤 Dormant: {len(dormant)} saved, not committed. Newest: "
                   + ", ".join(ln.slug for ln in recent))
    if archived:
        out.append(f"🗄 Archived: {len(archived)}")
    return "\n".join(out)


def _list(store: LaneStore, s: Settings, status: Optional[str]) -> str:
    ref = now()
    groups = [status] if status else [ACTIVE, DORMANT, ARCHIVED]
    out = []
    for st in groups:
        lanes = store.by_status(st)
        out.append(f"{st.upper()} ({len(lanes)})")
        out += [_line(ln, ref, s.stale_days) for ln in lanes] or ["• none"]
    return "\n".join(out)


def _show(store: LaneStore, s: Settings, query: str) -> str:
    ln = store.find(query)
    entries = ln.log_entries()[-8:]
    out = [f"{ln.title} (`{ln.slug}`): {ln.status.upper()} · updated {_ago(ln.age_days())}"]
    if ln.why:
        out.append(f"why: {ln.why}")
    out.append(f"next: {ln.next_action or '(none set)'}")
    if entries:
        out.append("recent log:")
        out += [f"  {e}" for e in entries]
    out.append(f"file: {store.root / (ln.slug + '.md')}")
    return "\n".join(out)


def _promote(store: LaneStore, s: Settings, args: list[str]) -> str:
    park_q = None
    if "--park" in args:
        i = args.index("--park")
        park_q = " ".join(args[i + 1:]).strip()
        args = args[:i]
        if not park_q:
            raise LaneError("--park needs a lane name.")
    target = store.find(" ".join(args))
    if target.status == ACTIVE:
        raise LaneError(f"`{target.slug}` is already active.")
    active = store.by_status(ACTIVE)
    parked = None
    if park_q:
        parked = store.find(park_q)
        if parked.status != ACTIVE:
            raise LaneError(f"`{parked.slug}` isn't active, so there's nothing to park.")
    if len(active) - (1 if parked else 0) >= s.max_active:
        names = ", ".join(f"`{ln.slug}`" for ln in active)
        if len(active) > s.max_active:
            raise LaneError(
                f"You're already over the active-lane cap ({len(active)}/{s.max_active}): {names}.\n"
                f"Park {len(active) - s.max_active + 1} first (/lanes park <lane>), then promote `{target.slug}`. "
                f"Or raise the cap in the plugin settings (max_active).")
        raise LaneError(
            f"You're at the active-lane cap ({len(active)}/{s.max_active}): {names}.\n"
            f"Promoting `{target.slug}` means parking one of them. Run:\n"
            f"/lanes promote {target.slug} --park <lane>")
    if parked:
        store.set_status(parked, DORMANT, reason=f"parked to make room for {target.slug}")
    store.set_status(target, ACTIVE, reason="promoted")
    msg = f"✅ `{target.slug}` is now ACTIVE ({len(store.by_status(ACTIVE))}/{s.max_active})."
    if parked:
        msg += f"\n💤 `{parked.slug}` parked to dormant. Its notes are kept."
    if not target.next_action:
        msg += f"\nSet its first next action: /lanes next {target.slug} -- <action>"
    return msg


WRITE_VERBS = {"new", "add", "capture", "promote", "park", "pause", "archive", "revive",
               "unarchive", "next", "log", "note"}


def run_command(store: LaneStore, s: Settings, raw: str, *, read_only: bool = False) -> str:
    try:
        args = shlex.split(raw or "")
    except ValueError:
        args = (raw or "").split()
    if not args:
        return brief(store, s)
    verb, rest = args[0].lower(), args[1:]
    if read_only and verb in WRITE_VERBS:
        return (f"`hermes lanes {verb}` is read-only from a terminal, because an agent with shell "
                f"access could otherwise run it. Type `/lanes {raw.strip()}` in a Hermes chat instead.")
    try:
        if verb in {"help", "-h", "--help"}:
            return HELP
        if verb in {"all", "list", "ls"}:
            return _list(store, s, None)
        if verb in {ACTIVE, DORMANT, ARCHIVED}:
            return _list(store, s, verb)
        if verb == "show":
            return _show(store, s, " ".join(rest))
        if verb in {"new", "add", "capture"}:
            head, why = _split_tail(rest)
            title = " ".join(head)
            dup = store.similar(title)
            if dup:
                names = ", ".join(f"`{d.slug}` ({d.status})" for d in dup[:3])
                return (f"Looks like this already exists: {names}. "
                        f"Add to it with /lanes log <lane> -- <note>, or pick a more distinct title.")
            ln = store.create(title, why=why, source="user")
            return f"💤 Saved `{ln.slug}` as dormant. It is not an active commitment. Promote with /lanes promote {ln.slug}"
        if verb == "promote":
            return _promote(store, s, rest)
        if verb in {"park", "pause"}:
            head, reason = _split_tail(rest)
            ln = store.find(" ".join(head))
            if ln.status != ACTIVE:
                raise LaneError(f"`{ln.slug}` is {ln.status}, not active.")
            store.set_status(ln, DORMANT, reason=reason or "parked")
            return f"💤 `{ln.slug}` parked. Notes kept, no longer a commitment."
        if verb == "archive":
            head, reason = _split_tail(rest)
            ln = store.find(" ".join(head))
            store.set_status(ln, ARCHIVED, reason=reason or "archived")
            return f"🗄 `{ln.slug}` archived. The file is kept at {store.root / (ln.slug + '.md')}"
        if verb in {"revive", "unarchive"}:
            ln = store.find(" ".join(rest))
            if ln.status != ARCHIVED:
                raise LaneError(f"`{ln.slug}` isn't archived.")
            store.set_status(ln, DORMANT, reason="revived")
            return f"💤 `{ln.slug}` is back in the dormant vault."
        if verb == "next":
            head, action = _split_tail(rest)
            if not action:
                raise LaneError("Usage: /lanes next <lane> -- <next action>")
            ln = store.find(" ".join(head))
            store.append_log(ln, "next", "", next_action=action)
            return f"➡️ `{ln.slug}` next: {action}"
        if verb in {"log", "note"}:
            head, note = _split_tail(rest)
            if not note:
                raise LaneError("Usage: /lanes log <lane> -- <note>")
            ln = store.find(" ".join(head))
            store.append_log(ln, "note", note)
            return f"📝 Logged on `{ln.slug}`."
        if verb == "where":
            return str(store.root)
        return f"Unknown /lanes command '{verb}'.\n\n{HELP}"
    except LaneError as exc:
        return f"⚠ {exc}"
