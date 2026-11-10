import json
from datetime import timedelta
from pathlib import Path

import pytest

import lane_keeper
from lane_keeper import core, tools
from lane_keeper.store import ACTIVE, ARCHIVED, DORMANT, LaneStore, Registry, parse_lane


def settings(tmp_path, **over):
    vals = dict(core.DEFAULTS, lanes_dir=str(tmp_path / "lanes"), **over)
    return core.Settings.load(lambda k, d: vals.get(k, d), lambda: tmp_path / "unused")


def mkstore(tmp_path, s):
    return LaneStore(s.lanes_dir, Registry(tmp_path / "data" / "registry.json"))


@pytest.fixture
def env(tmp_path):
    s = settings(tmp_path)
    return mkstore(tmp_path, s), s


def cmd(env, text, **kw):
    store, s = env
    return core.run_command(store, s, text, **kw)


def guard_for(tmp_path, s):
    return tools.make_guard(lambda: (s, [s.lanes_dir, tmp_path / "data"]))


# ---------------------------------------------------------------- basics

def test_new_lane_is_dormant_and_readable_markdown(env):
    store, _ = env
    out = cmd(env, 'new "Garage workshop" -- build a bench and pegboard')
    assert "dormant" in out
    ln = store.find("garage")
    assert ln.status == DORMANT
    text = (store.root / "garage-workshop.md").read_text()
    assert text.startswith("---\n") and "## Log" in text and "build a bench" in text


def test_promote_respects_cap_and_forces_tradeoff(tmp_path):
    s = settings(tmp_path, max_active=2)
    store = mkstore(tmp_path, s)
    for t in ("Alpha", "Bravo", "Charlie"):
        store.create(t)
    assert "ACTIVE" in core.run_command(store, s, "promote alpha")
    assert "ACTIVE" in core.run_command(store, s, "promote bravo")
    blocked = core.run_command(store, s, "promote charlie")
    assert "cap" in blocked and "--park" in blocked
    assert store.find("charlie").status == DORMANT
    out = core.run_command(store, s, "promote charlie --park alpha")
    assert "parked" in out
    assert store.find("charlie").status == ACTIVE
    assert store.find("alpha").status == DORMANT
    assert len(store.by_status(ACTIVE)) == 2


def test_promote_never_waits_for_input(tmp_path):
    """Rule 12: at the cap, promote returns an instruction immediately; it never prompts."""
    s = settings(tmp_path, max_active=1)
    store = mkstore(tmp_path, s)
    store.create("One", status=ACTIVE)
    store.create("Two")
    out = core.run_command(store, s, "promote two")
    assert isinstance(out, str) and "--park" in out


def test_park_archive_revive_and_log(env):
    store, _ = env
    store.create("Pertzy review")
    cmd(env, "promote pertzy")
    assert "parked" in cmd(env, "park pertzy -- waiting on partner")
    assert "archived" in cmd(env, "archive pertzy")
    assert store.find("pertzy").status == ARCHIVED
    assert "dormant" in cmd(env, "revive pertzy")
    log = store.find("pertzy").log_entries()
    assert any("active → dormant" in e and "waiting on partner" in e for e in log)


def test_next_action_and_show(env):
    store, _ = env
    store.create("Dream Builder")
    cmd(env, "next dream -- record take 2 of the gratitude track")
    assert store.find("dream").next_action == "record take 2 of the gratitude track"
    assert "record take 2" in cmd(env, "show dream")


def test_duplicate_guard_on_new(env):
    store, _ = env
    store.create("Paying Blue review")
    out = cmd(env, "new Paying Blue Review")
    assert "already exists" in out
    assert len(store.all()) == 1


def test_ambiguous_and_missing_lookup(env):
    store, _ = env
    store.create("Project Apple")
    store.create("Project Apricot")
    assert "more than one" in cmd(env, "show project ap")
    assert "No lane matches" in cmd(env, "show zebra")


def test_round_trip_preserves_unknown_front_matter(env):
    store, _ = env
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "x.md").write_text("---\ntitle: X\nstatus: dormant\ntags: [a, b]\n---\n# X\n\n## Log\n")
    cmd(env, "log x -- hello")
    text = (store.root / "x.md").read_text()
    assert "tags:" in text and "hello" in text


def test_empty_state_messages(env):
    store, s = env
    assert "No lanes yet" in cmd(env, "")
    assert core.context_block(store, s, first_turn=False) is None
    assert "No lanes yet" in core.context_block(store, s, first_turn=True)
    assert "Unknown /lanes command" in cmd(env, "frobnicate")


# ------------------------------------------------- the gate: every path the agent has

def test_gate_registry_bootstraps_once_from_existing_files(tmp_path):
    s = settings(tmp_path)
    s.lanes_dir.mkdir(parents=True)
    (s.lanes_dir / "old.md").write_text("---\ntitle: Old\nstatus: active\n---\nbody\n")
    store = mkstore(tmp_path, s)
    assert store.find("old").status == ACTIVE  # adopted on first sight
    # after bootstrap, a NEW file claiming active is not active
    (s.lanes_dir / "sneaky.md").write_text("---\ntitle: Sneaky\nstatus: active\n---\nbody\n")
    ln = mkstore(tmp_path, s).find("sneaky")
    assert ln.status == DORMANT and ln.mismatch


def test_gate_editing_a_file_to_active_does_not_promote(env):
    store, s = env
    store.create("Idea")
    path = store.root / "idea.md"
    path.write_text(path.read_text().replace("status: dormant", "status: active"))
    ln = store.find("idea")
    assert ln.status == DORMANT and ln.file_status == ACTIVE and ln.mismatch
    assert len(store.by_status(ACTIVE)) == 0
    assert "Ignored status edits" in core.brief(store, s)
    assert "STATUS MISMATCH" in core.snapshot(store, s)
    # the user can still promote it for real, which also repairs the file
    assert "ACTIVE" in cmd(env, "promote idea")
    assert not store.find("idea").mismatch


def test_gate_file_edits_cannot_break_the_cap(tmp_path):
    s = settings(tmp_path, max_active=1)
    store = mkstore(tmp_path, s)
    store.create("Real", status=ACTIVE)
    for t in ("Fake one", "Fake two", "Fake three"):
        ln = store.create(t)
        p = store.root / f"{ln.slug}.md"
        p.write_text(p.read_text().replace("status: dormant", "status: active"))
    assert [ln.slug for ln in store.by_status(ACTIVE)] == ["real"]
    assert "OVER CAP" not in core.snapshot(store, s)


def test_gate_checkpoint_on_a_tampered_lane_restores_the_real_status(env):
    store, _ = env
    store.create("Idea")
    p = store.root / "idea.md"
    p.write_text(p.read_text().replace("status: dormant", "status: active"))
    _, _, checkpoint = tools.make_handlers(lambda: store)
    json.loads(checkpoint({"lane": "idea", "kind": "note", "note": "x"}))
    assert "status: dormant" in p.read_text()


def test_gate_cli_is_read_only(env):
    store, _ = env
    store.create("Alpha")
    for verb in ("promote alpha", "park alpha", "archive alpha", "revive alpha",
                 "new Beta", "next alpha -- x", "log alpha -- x"):
        out = cmd(env, verb, read_only=True)
        assert "read-only" in out, verb
    assert store.find("alpha").status == DORMANT and len(store.all()) == 1
    assert "Alpha" in cmd(env, "all", read_only=True)
    assert "alpha" in cmd(env, "show alpha", read_only=True)


def test_gate_tools_cannot_change_status(env):
    store, _ = env
    lane_list, capture, checkpoint = tools.make_handlers(lambda: store)
    schemas = (tools.LANE_LIST, tools.LANE_CAPTURE, tools.LANE_CHECKPOINT)
    for sch in schemas:  # no tool exposes a status argument
        props = sch["parameters"].get("properties", {})
        assert "status" not in props or sch["name"] == "lane_list"
    r = json.loads(capture({"title": "Boat", "summary": "x", "status": "active"}))
    assert r["lane"]["status"] == DORMANT
    json.loads(checkpoint({"lane": "boat", "kind": "decision", "note": "go", "status": "active"}))
    assert store.find("boat").status == DORMANT


def test_gate_guard_blocks_file_shell_and_cli_paths(env, tmp_path):
    store, s = env
    store.create("Alpha")
    guard = guard_for(tmp_path, s)
    lane_file = str(store.root / "alpha.md")
    registry = str(tmp_path / "data" / "registry.json")
    blocked = lambda tool, **a: (guard(tool_name=tool, args=a) or {}).get("action") == "block"
    assert blocked("write_file", path=lane_file, content="status: active")
    assert blocked("patch", path=lane_file)
    assert blocked("write_file", path=registry, content="{}")  # the real state is protected too
    assert blocked("terminal", command=f"sed -i '' s/dormant/active/ {lane_file}")
    assert blocked("terminal", command=f"echo status: active | tee {lane_file}")
    assert blocked("terminal", command=f"echo x >> {lane_file}")
    assert blocked("terminal", command=f"rm {lane_file}")
    assert blocked("terminal", command=f"python3 -c 'open(\"{registry}\",\"w\")'")
    for c in ("hermes lanes promote alpha", "cd ~ && hermes lanes park alpha -- x",
              "hermes config set plugins.entries.lane-keeper.settings.max_active 9",
              "hermes plugins disable lane-keeper"):
        assert blocked("terminal", command=c), c
    # reads and unrelated work are fine
    assert guard(tool_name="terminal", args={"command": f"cat {lane_file}"}) is None
    assert guard(tool_name="terminal", args={"command": f"grep -h Log {lane_file} 2>/dev/null"}) is None
    assert guard(tool_name="terminal", args={"command": f"mv a b; cat {lane_file} 2>&1 | tail -3"}) is None
    assert guard(tool_name="terminal", args={"command": "hermes lanes show alpha"}) is None
    assert guard(tool_name="write_file", args={"path": str(tmp_path / "other.md")}) is None
    assert guard(tool_name="read_file", args={"path": lane_file}) is None
    off = settings(tmp_path, guard_lane_files=False)
    assert guard_for(tmp_path, off)(tool_name="write_file", args={"path": lane_file}) is None


# ------------------------------------------------------- what the model sees

def test_snapshot_marks_dormant_as_not_commitments(env):
    store, s = env
    store.create("Idea one")
    store.create("Live work", status=ACTIVE)
    snap = core.snapshot(store, s)
    assert "Active lanes (1/3)" in snap and "NOT commitments" in snap and "Idea one" in snap


def test_over_cap_is_surfaced_not_auto_fixed(tmp_path):
    s = settings(tmp_path, max_active=2)
    store = mkstore(tmp_path, s)
    for t in ("A one", "B two", "C three"):
        store.create(t, status=ACTIVE)
    assert "OVER CAP" in core.snapshot(store, s)
    assert "Over cap" in core.brief(store, s)
    assert len(store.by_status(ACTIVE)) == 3


def test_stale_notice_is_given_once_and_survives_restart(tmp_path):
    s = settings(tmp_path)
    store = mkstore(tmp_path, s)
    ln = store.create("Old lane", status=ACTIVE)
    ln.updated = ln.updated - timedelta(days=10)
    store._write(ln)
    assert "STALE 10d (mention once)" in core.snapshot(store, s)
    restarted = mkstore(tmp_path, s)  # fresh objects, as after a gateway restart
    snap = core.snapshot(restarted, s)
    assert "mention once" not in snap and "already mentioned" in snap
    assert "stale" in core.brief(restarted, s)
    # new activity resets it, so a later lapse gets one fresh notice
    cmd((restarted, s), "log old -- moved it")
    ln = restarted.find("old")
    ln.updated = ln.updated - timedelta(days=9)
    restarted._write(ln)
    assert "mention once" in core.snapshot(mkstore(tmp_path, s), s)


def test_session_snapshot_then_delta_only_on_change(env):
    store, s = env
    store.create("Alpha")
    section = core.system_section(store, s, "sess-1")
    assert section.startswith("Lane Keeper is on.") and "Alpha" in section
    assert core.turn_delta(store, s, "sess-1") is None      # nothing changed
    assert core.turn_delta(store, s, "sess-1") is None
    cmd(env, "promote alpha")
    delta = core.turn_delta(store, s, "sess-1")
    assert delta and "Lanes changed" in delta and "Alpha" in delta and "\n" not in delta
    assert core.turn_delta(store, s, "sess-1") is None      # reported once
    assert core.turn_delta(store, s, "unknown-session") is None


def test_section_fits_the_host_budget_with_many_lanes(tmp_path):
    s = settings(tmp_path, max_active=20)
    store = mkstore(tmp_path, s)
    for i in range(15):
        store.create(f"Active lane number {i} with a long descriptive title", status=ACTIVE,
                     next_action="a fairly long next action sentence " * 3)
    for i in range(40):
        store.create(f"Dormant idea {i} with a reasonably long name")
    assert len(core.system_section(store, s, "x")) <= 4000


def test_hand_edited_file_with_bad_status_is_not_active(env):
    store, _ = env
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "weird.md").write_text("---\ntitle: Weird\nstatus: urgent!!\n---\nbody\n")
    (store.root / "plain.md").write_text("just a note, no front matter\n")
    lanes = {ln.slug: ln for ln in store.all()}
    assert lanes["weird"].status == DORMANT and lanes["plain"].status == DORMANT
    assert parse_lane(store.root / "plain.md").title == "Plain"


# ------------------------------------------------- wiring through register(ctx)

class FakeCtx:
    def __init__(self, tmp_path, **cfg):
        self.cfg = dict(lanes_dir=str(tmp_path / "lanes"), **cfg)
        self.hooks, self.tools, self.commands, self.sections, self.cli = {}, {}, {}, {}, {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_system_prompt_section(self, id, content, **kw):
        self.sections[id] = content

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, **kw):
        self.cli[name] = kw


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_keeper, "_data_dir", lambda: tmp_path / "data")
    ctx = FakeCtx(tmp_path)
    lane_keeper.register(ctx)
    return ctx


def test_register_wires_everything(wired):
    assert set(wired.tools) == {"lane_list", "lane_capture", "lane_checkpoint"}
    assert set(wired.hooks) == {"pre_llm_call", "pre_tool_call"}
    assert "lanes" in wired.commands and "lanes" in wired.cli and "lane-keeper" in wired.sections


def test_end_to_end_capture_then_user_promotes(wired):
    section = wired.sections["lane-keeper"]
    assert "No lanes yet" in section({"session_id": "s1", "platform": "telegram"})
    r = json.loads(wired.tools["lane_capture"]({"title": "Backyard sauna", "summary": "winter build"}))
    assert r["lane"]["status"] == DORMANT
    out = wired.hooks["pre_llm_call"](session_id="s1", platform="telegram")
    assert out and "Lanes changed" in out["context"]
    assert wired.hooks["pre_llm_call"](session_id="s1", platform="telegram") is None
    assert "ACTIVE" in wired.commands["lanes"]("promote sauna")


def test_unattended_runs_get_no_lane_context(wired):
    section = wired.sections["lane-keeper"]
    assert section({"session_id": "c1", "platform": "cron"}) is None
    assert wired.hooks["pre_llm_call"](session_id="c1", platform="cron") is None
