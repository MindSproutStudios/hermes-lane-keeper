"""Lane Keeper: keep an agent focused on your active lanes without losing your other ideas.

Surfaces used (all public plugin APIs):
- register_system_prompt_section: rules + lane snapshot, rendered once per session (cache-safe)
- pre_llm_call: a one-line update only when lanes change mid-session
- pre_tool_call: guard that stops the agent rewriting lane files or Lane Keeper's own data
- tools: lane_list, lane_capture, lane_checkpoint (no status-changing tool exists)
- /lanes slash command: the only way to promote/park/archive
- `hermes lanes` CLI: read-only
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import shlex
from pathlib import Path

from . import core, tools
from .store import LaneStore, Registry

logger = logging.getLogger(__name__)

PLUGIN_NAME = "lane-keeper"
SECTION_MAX = 4000


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN_NAME))
    except Exception:
        try:
            from hermes_constants import get_hermes_home
            return Path(get_hermes_home()) / "plugin-data" / PLUGIN_NAME
        except Exception:
            return Path.home() / ".hermes" / "plugin-data" / PLUGIN_NAME


def register(ctx):
    def settings() -> core.Settings:
        return core.Settings.load(lambda k, d: ctx.get_config(k, default=d), lambda: _data_dir() / "lanes")

    def store(s: core.Settings | None = None) -> LaneStore:
        s = s or settings()
        # One registry per lanes folder, kept in the plugin's own data dir (never in lanes_dir).
        key = hashlib.sha256(str(s.lanes_dir.expanduser().resolve()).encode()).hexdigest()[:10]
        return LaneStore(s.lanes_dir, Registry(_data_dir() / f"registry-{key}.json"))

    def protected_roots():
        s = settings()
        return s, [s.lanes_dir, _data_dir()]

    # 1. Rules + lane snapshot, frozen once per session.
    def section(info=None):
        try:
            info = dict(info or {})
            if str(info.get("platform") or "") in core.UNATTENDED_PLATFORMS:
                return None  # cron/batch runs don't capture ideas or see lanes
            s = settings()
            if s.inject == "off":
                return None
            return core.system_section(store(s), s, str(info.get("session_id") or ""))[:SECTION_MAX]
        except Exception as exc:
            logger.warning("lane-keeper: prompt section skipped: %s", exc)
            return None

    has_section = False
    if hasattr(ctx, "register_system_prompt_section"):
        try:
            ctx.register_system_prompt_section("lane-keeper", section, max_chars=SECTION_MAX)
            has_section = True
        except Exception as exc:
            logger.warning("lane-keeper: system prompt sections unavailable (%s); using per-turn context", exc)

    # 2. Mid-session changes only (or the full block on hosts without sections).
    def inject(session_id: str = "", is_first_turn: bool = False, platform: str = "", **kwargs):
        try:
            if str(platform or "") in core.UNATTENDED_PLATFORMS:
                return None
            s = settings()
            if s.inject == "off":
                return None
            st = store(s)
            if not has_section:
                block = core.context_block(st, s, first_turn=bool(is_first_turn))
                return {"context": core.RULES + "\n\n" + block} if block else None
            if s.inject == "every_turn":
                return {"context": core.snapshot(st, s, mark_stale=False)}
            delta = core.turn_delta(st, s, session_id)
            return {"context": delta} if delta else None
        except Exception as exc:
            logger.warning("lane-keeper: context injection skipped: %s", exc)
            return None

    ctx.register_hook("pre_llm_call", inject)

    # 3. Guard lane files and Lane Keeper's data against direct edits by the agent.
    ctx.register_hook("pre_tool_call", tools.make_guard(protected_roots))

    # 4. Agent tools: list, capture (always dormant), checkpoint. None change status.
    lane_list, lane_capture, lane_checkpoint = tools.make_handlers(store)
    ctx.register_tool(name="lane_list", toolset="lane-keeper", schema=tools.LANE_LIST, handler=lane_list)
    ctx.register_tool(name="lane_capture", toolset="lane-keeper", schema=tools.LANE_CAPTURE, handler=lane_capture)
    ctx.register_tool(name="lane_checkpoint", toolset="lane-keeper", schema=tools.LANE_CHECKPOINT, handler=lane_checkpoint)

    # 5. The user's controls. Slash commands come only from a person's message.
    def slash(raw_args: str = "", **kwargs) -> str:
        try:
            s = settings()
            return core.run_command(store(s), s, raw_args or "")
        except Exception as exc:
            logger.exception("lane-keeper: /lanes failed")
            return f"⚠ /lanes failed: {exc}"

    ctx.register_command("lanes", handler=slash,
                         description="Your active lanes, dormant ideas, and promote/park controls",
                         args_hint="[promote|park|archive|new|show|next|log|all] …")

    # 6. Terminal: read-only, so an agent with shell access can't promote.
    def _setup(parser):
        parser.add_argument("words", nargs=argparse.REMAINDER,
                            help="Read-only: `hermes lanes`, `hermes lanes all`, `hermes lanes show <lane>`")
        parser.set_defaults(func=_cli)

    def _cli(args):
        s = settings()
        words = list(getattr(args, "words", None) or [])
        print(core.run_command(store(s), s, shlex.join(words), read_only=True))

    ctx.register_cli_command(name="lanes", help="Lane Keeper: view your lanes (read-only)",
                             setup_fn=_setup, handler_fn=_cli)
