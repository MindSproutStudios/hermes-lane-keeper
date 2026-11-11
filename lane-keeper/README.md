# Lane Keeper

**Your agent remembers every idea you have. Lane Keeper makes sure it only works on the ones you chose.**

If you think out loud with an agent, it turns every idea into work: a new folder, a plan, a research spree. A week later you have twelve half-started projects and the three that mattered haven't moved.

Lane Keeper gives Hermes one rule and enforces it:

> Preserve everything useful. Execute only what is active. Promote dormant ideas deliberately.

**It's not a task tracker.** Kanban and project plugins organize work. Lane Keeper is a hard limit on what the agent is allowed to treat as work, and only you can change it.

## How it works

You keep a few **active lanes** (current commitments, capped at 3 by default) and a vault of **dormant lanes** (saved ideas, not commitments).

- **The agent knows your lanes.** Each session starts with your active lanes, their next actions, and anything stale or over the cap.
- **New ideas get saved instead of started.** Mention a backyard sauna in the middle of something else and the agent files it as a dormant lane in one line. If you explicitly ask it to work on it now, it does the work and tells you the idea sits outside your active lanes.
- **Only you promote.** The agent has no tool that changes lane status. Promotion happens when you type `/lanes promote` in chat. At the cap, promoting means choosing what to park: `/lanes promote sauna --park garage`.
- **Stale lanes get called out once.** If an active lane has had no update in 7 days, the agent mentions it once, and that state survives restarts. Then you push it forward or park it.
- **Decisions survive compression.** The agent records decisions, blockers and next actions on the lane with `lane_checkpoint`, so they live in a file instead of a chat window.
- **Plain markdown.** Each lane is a file with a small YAML header and an append-only log. Point `lanes_dir` at your notes or Obsidian vault.

## The gate: why the agent can't promote

Every path an agent could use to make a lane active is closed, and each one has a test:

| Path | What happens | Test |
| --- | --- | --- |
| A tool call | No tool can set status: `lane_capture` always creates dormant lanes, and `lane_checkpoint` never changes status | `test_gate_tools_cannot_change_status` |
| Editing a lane file to `status: active` | Status lives in a registry in Lane Keeper's own data folder; the markdown is a mirror. A file claiming a status it was never given is ignored, reported to you, and repaired on the next write | `test_gate_editing_a_file_to_active_does_not_promote`, `test_gate_checkpoint_on_a_tampered_lane_restores_the_real_status` |
| Dropping in new files marked active | Unknown files are dormant. File edits can't push you over the cap | `test_gate_registry_bootstraps_once_from_existing_files`, `test_gate_file_edits_cannot_break_the_cap` |
| `hermes lanes promote` in a terminal | The CLI is read-only. Status changes happen only through `/lanes` in chat, which comes from a person's message | `test_gate_cli_is_read_only` |
| Writing files, the registry, settings, or disabling the plugin via shell | Blocked by a `pre_tool_call` guard | `test_gate_guard_blocks_file_shell_and_cli_paths` |

The guard is a policy seam, not a sandbox: code the agent writes and runs some other way is outside its view. The registry is what makes the gate hold even then. A file edit, however it happens, doesn't change status.

The first time Lane Keeper sees a lanes folder, it adopts the statuses already in the files once. That way an existing notes folder or Obsidian vault keeps its state.

## Commands

Type these in any Hermes chat (CLI, Desktop, Telegram, Discord, …):

```
/lanes                                 brief: active lanes, next actions, stale / over-cap / ignored-edit flags
/lanes all | dormant | archived        list lanes
/lanes show <lane>                     one lane with its recent log
/lanes new <title> [-- why]            save a dormant lane
/lanes promote <lane> [--park <lane>]  make a lane active (cap-aware; never waits for input)
/lanes park <lane> [-- reason]         active → dormant
/lanes archive <lane> [-- reason]      → archived (file kept)
/lanes revive <lane>                   archived → dormant
/lanes next <lane> -- <action>         set the next action
/lanes log <lane> -- <note>            add a log line
```

`hermes lanes`, `hermes lanes all` and `hermes lanes show <lane>` work in a terminal, read-only.

Lane names resolve by slug, title, unique prefix or close match (`/lanes show pay` finds `paying-blue-project-review`).

## Agent tools

| Tool | What it does | Can it change status? |
| --- | --- | --- |
| `lane_list` | Full lane detail | No |
| `lane_capture` | Save a new idea as dormant; returns similar lanes instead of duplicating | No, always dormant |
| `lane_checkpoint` | Append a decision/progress/blocker/note and optionally a new next action | No |

## Why it's a plugin and not a skill

A skill is a suggestion the model can forget halfway through a long session. Lane Keeper enforces the rule through public plugin surfaces only:

- `register_system_prompt_section` renders the rules plus a lane snapshot (capped at about 4,000 characters) once per session. The text is then frozen, so the prompt cache stays valid.
- The `pre_llm_call` hook adds a one-line note only when lanes change mid-session (a capture, a promotion, a park), and returns nothing otherwise.
- The `pre_tool_call` hook is the guard described above.
- `/lanes` (`register_command`) is the user's controls. `hermes lanes` (`register_cli_command`) is read-only.
- The status registry, the stale notices already given and per-session snapshot fingerprints are stored in `plugin-data/lane-keeper/registry-<hash>.json`.

Cron, batch and webhook runs get no lane context, so unattended jobs don't capture ideas.

## Settings

Set these in Desktop → Settings → Plugins → Lane Keeper, or under `plugins.entries.lane-keeper.settings` in `config.yaml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `lanes_dir` | `<HERMES_HOME>/plugin-data/lane-keeper/lanes` | Where lane markdown files live |
| `max_active` | `3` | Active-lane cap |
| `stale_days` | `7` | Days without an update before an active lane is flagged |
| `inject` | `changes` | `changes` (snapshot per session + one line on change), `every_turn`, or `off` |
| `dormant_in_context` | `15` | Dormant titles the agent sees, for spotting duplicates. `0` = count only |
| `guard_lane_files` | `true` | Block the agent's file, shell and CLI edits of lane data |

## Install

```bash
hermes plugins install lane-keeper      # from the catalog, once listed
hermes plugins enable lane-keeper
```

Then start a new session and type `/lanes`.

## Disclosure

- **Network:** none. **Subprocesses:** none. **LLM calls of its own:** none. **Telemetry:** none.
- **Credentials:** never reads `.env`, `auth.json`, keychains or token files.
- **Reads and writes:**
  - markdown files in `lanes_dir` (default: inside its own plugin-data folder). If you point `lanes_dir` at a notes or Obsidian folder, it reads and writes `*.md` files in that folder only, not subfolders.
  - one JSON file, `<HERMES_HOME>/plugin-data/lane-keeper/registry-<hash>.json`, holding lane statuses, stale notices already given, and per-session fingerprints (at most 300).
- **Sent to your model provider:** in the system prompt, once per session, a rules section plus active lane titles, slugs and next actions, staleness, and up to 15 dormant titles. In the user message, a one-line update only when lanes change mid-session.
- **Hooks:** `pre_tool_call` reads tool names and arguments to decide whether to block writes to lane data, status-changing `hermes lanes` commands, Lane Keeper settings changes, or disabling the plugin. It stores nothing. `pre_llm_call` reads the session id and platform.
- **Background work:** none. **Self-update:** none.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pytest pyyaml
.venv/bin/python -m pytest              # 26 tests, including every gate path above
hermes plugins validate lane-keeper
```

## Origin

Built by Morris, a Hermes agent, from the operating rule it runs under for its user. When Lane Keeper was first switched on, his real setup had 7 "active" lanes against a stated cap of 3. That drift is the problem this plugin exists to surface, and it was the first thing it caught.

MIT licensed.
