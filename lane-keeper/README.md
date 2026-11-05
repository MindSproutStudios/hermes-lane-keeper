# Lane Keeper

**Your agent remembers every idea you have. Lane Keeper makes sure it only works on the ones you chose.**

If you think out loud with an agent, it will happily turn every idea into work: a new folder, a plan, a research spree. A week later you have twelve half-started projects and the three that mattered haven't moved.

Lane Keeper gives Hermes one rule and enforces it:

> Preserve everything useful. Execute only what is active. Promote dormant ideas deliberately.

## How it works

You keep a few **active lanes** (current commitments, capped at 3 by default) and a vault of **dormant lanes** (saved ideas, not commitments).

- **The agent always knows your lanes.** Every turn it sees your active lanes, their next actions, and which ones have gone stale.
- **New ideas get saved instead of started.** Mention a backyard sauna in the middle of something else and the agent files it as a dormant lane in one line. If you explicitly ask it to work on it now, it does the work and tells you the idea sits outside your active lanes.
- **Only you promote.** The model has no tool that changes lane status. Promotion happens when you type `/lanes promote`. At the cap, promoting means choosing what to park: `/lanes promote sauna --park garage`.
- **Stale lanes get called out.** If an active lane has had no update in 7 days, the agent mentions it once, so you push it forward or park it honestly.
- **Decisions survive compression.** The agent records decisions, blockers and next actions on the lane with `lane_checkpoint`, so they live in a file instead of a chat window.
- **Plain markdown.** Each lane is a file with a small YAML header and an append-only log. Point `lanes_dir` at your notes or Obsidian vault and read or edit lanes like any other note.

## Commands

Type these in any Hermes chat (CLI, Desktop, Telegram, Discord, …), or run them as `hermes lanes …` in a terminal.

```
/lanes                                 brief: active lanes, next actions, stale and over-cap flags
/lanes all | dormant | archived        list lanes
/lanes show <lane>                     one lane with its recent log
/lanes new <title> [-- why]            save a dormant lane
/lanes promote <lane> [--park <lane>]  make a lane active (cap-aware)
/lanes park <lane> [-- reason]         active → dormant
/lanes archive <lane> [-- reason]      → archived (file kept)
/lanes revive <lane>                   archived → dormant
/lanes next <lane> -- <action>         set the next action
/lanes log <lane> -- <note>            add a log line
```

Lane names resolve by slug, title, unique prefix or close match (`/lanes show pay` finds `paying-blue-project-review`).

## Agent tools

| Tool | What it does | Can it change status? |
| --- | --- | --- |
| `lane_list` | Full lane detail when the per-turn summary isn't enough | No |
| `lane_capture` | Save a new idea as dormant; returns similar lanes instead of duplicating | No, always dormant |
| `lane_checkpoint` | Append a decision/progress/blocker/note and optionally a new next action | No |

## Why it's a plugin and not a skill

A skill is a suggestion the model can forget halfway through a long session. Lane Keeper uses the plugin surface to enforce the rule:

- `register_system_prompt_section`: the rules are frozen into the system prompt once per session, so they don't break the prompt cache.
- `pre_llm_call`: live lane state is appended to each turn, so the agent never works from a stale picture.
- `pre_tool_call`: a guard blocks the agent from rewriting lane files with `write_file`/`patch`/shell writes, and from running status-changing `hermes lanes` commands or editing Lane Keeper's settings itself.
- `/lanes` slash command and `hermes lanes` CLI: the user's controls, the only path to a status change.
- Durable state in markdown files under `plugin-data/lane-keeper/lanes` (or your own folder).

The guard is a policy seam, not a sandbox. It catches the normal ways an agent would edit a lane (file tools, shell writes, the CLI). Code the agent writes and runs elsewhere is outside its view.

## Settings

Set these in Desktop → Settings → Plugins → Lane Keeper, or under `plugins.entries.lane-keeper.settings` in `config.yaml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `lanes_dir` | `<HERMES_HOME>/plugin-data/lane-keeper/lanes` | Where lane files live |
| `max_active` | `3` | Active-lane cap |
| `stale_days` | `7` | Days without an update before an active lane is flagged |
| `inject` | `every_turn` | `every_turn`, `first_turn` or `off` |
| `dormant_in_context` | `15` | Dormant titles the agent sees (for duplicate spotting); `0` = count only |
| `guard_lane_files` | `true` | Block direct edits of lane files by the agent |

## Install

```bash
hermes plugins install lane-keeper      # from the catalog, once listed
hermes plugins enable lane-keeper
```

Then start a new session and type `/lanes`.

## Disclosure

No network calls, no credentials, no LLM calls of its own. It reads and writes markdown files only in `lanes_dir`. Each turn it adds a short lane summary (titles, next actions, staleness) to the user message sent to your configured model. The system prompt gains a short rules section. The `pre_tool_call` hook reads tool names and arguments to decide whether to block a lane-file write; it stores nothing.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pytest pyyaml
.venv/bin/python -m pytest
hermes plugins validate lane-keeper
```

## Origin

Built by Morris, a Hermes agent, from the operating rule it runs under for its user. When Lane Keeper was first switched on, his real setup had 7 "active" lanes against a stated cap of 3. That drift is the problem this plugin exists to surface, and it was the first thing it caught.

MIT licensed.
