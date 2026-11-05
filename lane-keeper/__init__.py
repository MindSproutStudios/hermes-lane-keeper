"""Lane Keeper: keep an agent focused on your active lanes without losing your other ideas.

Surfaces used (all public plugin APIs):
- register_system_prompt_section: the lane rules, frozen once per session (cache-friendly)
- pre_llm_call: live active/dormant lane state appended to each user turn
- pre_tool_call: guard that stops the model rewriting lane files directly
- tools: lane_list, lane_capture, lane_checkpoint (no status-changing tool exists)
- /lanes slash command and `hermes lanes` CLI: the only way to promote/park/archive
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import core, tools
from .store import LaneStore

logger = logging.getLogger(__name__)

PLUGIN_NAME = "lane-keeper"


def _default_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_NAME) / "lanes"
    except Exception:
        try:
            from hermes_constants import get_hermes_home
            return Path(get_hermes_home()) / "plugin-data" / PLUGIN_NAME / "lanes"
        except Exception:
            return Path.home() / ".hermes" / "plugin-data" / PLUGIN_NAME / "lanes"


def register(ctx):
    def settings() -> core.Settings:
        return core.Settings.load(lambda k, d: ctx.get_config(k, default=d), _default_dir)

    def store() -> LaneStore:
        return LaneStore(settings().lanes_dir)

    # 1. Rules: stable text in the system prompt when the host supports it.
    rules_in_prompt = False
    if hasattr(ctx, "register_system_prompt_section"):
        try:
            ctx.register_system_prompt_section("lane-keeper.rules", core.RULES)
            rules_in_prompt = True
        except Exception as exc:  # older host or id clash: fall back to per-turn rules
            logger.warning("lane-keeper: system prompt section unavailable (%s); rules go per turn", exc)

    # 2. Live lane state on every turn.
    def inject(session_id: str = "", user_message: str = "", is_first_turn: bool = False, **kwargs):
        try:
            s = settings()
            if s.inject == "off" or (s.inject == "first_turn" and not is_first_turn):
                return None
            block = core.context_block(LaneStore(s.lanes_dir), s, first_turn=bool(is_first_turn))
            if not block:
                return None
            if not rules_in_prompt:
                block = core.RULES + "\n\n" + block
            return {"context": block}
        except Exception as exc:
            logger.warning("lane-keeper: context injection skipped: %s", exc)
            return None

    ctx.register_hook("pre_llm_call", inject)

    # 3. Guard lane files against direct edits by the model.
    ctx.register_hook("pre_tool_call", tools.make_guard(settings))

    # 4. Model tools: capture and checkpoint, never status changes.
    lane_list, lane_capture, lane_checkpoint = tools.make_handlers(store)
    ctx.register_tool(name="lane_list", toolset="lane-keeper", schema=tools.LANE_LIST, handler=lane_list)
    ctx.register_tool(name="lane_capture", toolset="lane-keeper", schema=tools.LANE_CAPTURE, handler=lane_capture)
    ctx.register_tool(name="lane_checkpoint", toolset="lane-keeper", schema=tools.LANE_CHECKPOINT, handler=lane_checkpoint)

    # 5. The user's controls.
    def slash(raw_args: str = "", **kwargs) -> str:
        try:
            return core.run_command(store(), settings(), raw_args or "")
        except Exception as exc:
            logger.exception("lane-keeper: /lanes failed")
            return f"⚠ /lanes failed: {exc}"

    ctx.register_command("lanes", handler=slash,
                         description="Your active lanes, dormant ideas, and promote/park controls",
                         args_hint="[promote|park|archive|new|show|next|log|all] …")

    def _setup(parser):
        parser.add_argument("words", nargs=argparse.REMAINDER,
                            help="Same arguments as /lanes, e.g. `promote garage --park pertzy`")
        parser.set_defaults(func=_cli)

    def _cli(args):
        words = list(getattr(args, "words", None) or [])
        import shlex
        print(core.run_command(store(), settings(), shlex.join(words)))

    ctx.register_cli_command(name="lanes", help="Lane Keeper: view and manage your lanes",
                             setup_fn=_setup, handler_fn=_cli)
