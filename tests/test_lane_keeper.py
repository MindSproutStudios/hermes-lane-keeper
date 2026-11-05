import json
from datetime import timedelta

import pytest

from lane_keeper import core, tools
from lane_keeper.store import ACTIVE, ARCHIVED, DORMANT, LaneStore, parse_lane


def settings(tmp_path, **over):
    vals = dict(core.DEFAULTS, lanes_dir=str(tmp_path / "lanes"), **over)
    return core.Settings.load(lambda k, d: vals.get(k, d), lambda: tmp_path / "unused")


@pytest.fixture
def env(tmp_path):
    s = settings(tmp_path)
    return LaneStore(s.lanes_dir), s


def cmd(env, text):
    store, s = env
    return core.run_command(store, s, text)


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
    store = LaneStore(s.lanes_dir)
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


def test_stale_flag_in_context_and_brief(env):
    store, s = env
    ln = store.create("Old lane", status=ACTIVE)
    ln.updated = ln.updated - timedelta(days=10)
    store._write(ln)
    block = core.context_block(store, s, first_turn=True)
    assert "STALE 10d" in block and "Mention them once" in block
    assert "STALE" in core.context_block(store, s, first_turn=False)
    assert "Mention them once" not in core.context_block(store, s, first_turn=False)
    assert "stale" in core.brief(store, s)


def test_context_lists_dormant_as_not_commitments(env):
    store, s = env
    store.create("Idea one")
    store.create("Live work", status=ACTIVE)
    block = core.context_block(store, s, first_turn=False)
    assert "Active lanes (1/3)" in block
    assert "NOT commitments" in block and "Idea one" in block


def test_tools_capture_dedupe_and_never_promote(env):
    store, s = env
    lane_list, capture, checkpoint = tools.make_handlers(lambda: store)
    r = json.loads(capture({"title": "Aquarium humidity fix", "summary": "condensation in office"}))
    assert r["created"] and r["lane"]["status"] == DORMANT
    r2 = json.loads(capture({"title": "aquarium humidity fixes", "summary": "x"}))
    assert r2["created"] is False and r2["similar_existing"]
    r3 = json.loads(checkpoint({"lane": "aquarium", "kind": "decision", "note": "add dehumidifier",
                                "next_action": "price 50-pint units"}))
    assert r3["recorded"] and "did not make it active" in r3["note"]
    assert store.find("aquarium").status == DORMANT
    assert store.find("aquarium").next_action == "price 50-pint units"
    listed = json.loads(lane_list({"status": "dormant"}))
    assert len(listed["lanes"]) == 1
    assert json.loads(checkpoint({"lane": "nope", "kind": "note", "note": "x"}))["error"]


def test_guard_blocks_direct_edits(env, tmp_path):
    store, s = env
    store.create("Alpha")
    guard = tools.make_guard(lambda: s)
    lane_file = str(store.root / "alpha.md")
    assert guard(tool_name="write_file", args={"path": lane_file, "content": "status: active"})["action"] == "block"
    assert guard(tool_name="patch", args={"path": lane_file})["action"] == "block"
    assert guard(tool_name="terminal", args={"command": f"sed -i '' s/dormant/active/ {lane_file}"})["action"] == "block"
    assert guard(tool_name="terminal", args={"command": f"cat {lane_file}"}) is None
    # reads with harmless redirects / unrelated writes elsewhere in the chain are fine
    assert guard(tool_name="terminal", args={"command": f"grep -h Log {lane_file} 2>/dev/null"}) is None
    assert guard(tool_name="terminal", args={"command": f"mv a b; cat {lane_file} 2>&1 | tail -3"}) is None
    # writes into lane files are blocked, including via pipe/redirect
    assert guard(tool_name="terminal", args={"command": f"echo status: active | tee {lane_file}"})["action"] == "block"
    assert guard(tool_name="terminal", args={"command": f"echo x >> {lane_file}"})["action"] == "block"
    assert guard(tool_name="terminal", args={"command": f"rm {lane_file}"})["action"] == "block"
    # no routing around the user's controls via the CLI
    for c in ("hermes lanes promote alpha", "cd ~ && hermes lanes park alpha -- x",
              "hermes config set plugins.entries.lane-keeper.settings.max_active 9",
              "hermes plugins disable lane-keeper"):
        assert guard(tool_name="terminal", args={"command": c})["action"] == "block", c
    assert guard(tool_name="terminal", args={"command": "hermes lanes"}) is None
    assert guard(tool_name="terminal", args={"command": "hermes lanes show alpha"}) is None
    assert guard(tool_name="write_file", args={"path": str(tmp_path / "other.md")}) is None
    assert guard(tool_name="read_file", args={"path": lane_file}) is None
    off = settings(tmp_path, guard_lane_files=False)
    assert tools.make_guard(lambda: off)(tool_name="write_file", args={"path": lane_file}) is None


def test_hand_edited_file_with_bad_status_is_not_active(env):
    store, _ = env
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "weird.md").write_text("---\ntitle: Weird\nstatus: urgent!!\n---\nbody\n")
    (store.root / "plain.md").write_text("just a note, no front matter\n")
    lanes = {ln.slug: ln for ln in store.all()}
    assert lanes["weird"].status == DORMANT and lanes["plain"].status == DORMANT
    assert parse_lane(store.root / "plain.md").title == "Plain"


def test_round_trip_preserves_unknown_front_matter(env):
    store, _ = env
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "x.md").write_text("---\ntitle: X\nstatus: dormant\ntags: [a, b]\n---\n# X\n\n## Log\n")
    cmd(env, "log x -- hello")
    text = (store.root / "x.md").read_text()
    assert "tags:" in text and "hello" in text


def test_over_cap_is_surfaced_not_auto_fixed(tmp_path):
    s = settings(tmp_path, max_active=2)
    store = LaneStore(s.lanes_dir)
    for t in ("A one", "B two", "C three"):
        store.create(t, status=ACTIVE)
    assert "OVER CAP" in core.context_block(store, s, first_turn=False)
    assert "Over cap" in core.brief(store, s)
    assert len(store.by_status(ACTIVE)) == 3  # nothing parked automatically


def test_empty_state_messages(env):
    store, s = env
    assert "No lanes yet" in cmd(env, "")
    assert core.context_block(store, s, first_turn=False) is None
    assert "No lanes yet" in core.context_block(store, s, first_turn=True)
    assert "Unknown /lanes command" in cmd(env, "frobnicate")
