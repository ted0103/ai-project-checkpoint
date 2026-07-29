# project-checkpoint

**Say checkpoint. Resume from one small, integrity-checked Markdown file.**

`project-checkpoint` is a portable Agent Skill for Git projects. It replaces an owned `HANDOFF.md` with a compact repository-backed snapshot, then validates file integrity and detects Git drift before a fresh task resumes. It uses Python's standard library and Git—no daemon, server, connector, or runtime package.

## Install

With the [Skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add ted0103/project-checkpoint --skill project-checkpoint
```

Or copy [`skills/project-checkpoint`](skills/project-checkpoint) into the skills directory of any Agent Skills-compatible tool. Runtime requirements are Git and Python 3.10+; the CLI installer additionally requires Node.js/npm. Then say `checkpoint`, `resume project`, or explicitly invoke `$project-checkpoint`.

The agent selects one explicit Git worktree; the helper never guesses from an ancestor repository alone. Checkpointing records Git identity, bounded status and manifest data, a content-sensitive fingerprint, concise user-supplied state, observed verification, and one next action. Resume validates metadata, prose integrity, referenced files, and current repository freshness, then returns only the compact human context needed to continue. Narrative claims are reconciled by the agent and are not presented as machine-verified facts.

## Walkthrough

In a disposable example repository, edit `src/app.py`, then say `checkpoint`. The agent resolves that repository, records the dirty path and its content identity, and publishes `HANDOFF.md`. In a fresh task, say `resume project`: unchanged Git state produces a compact orientation and next action. Editing `src/app.py` first produces a drift report instead. This is an illustrative local workflow, not a production result claim.

## Scope

Orchestration systems such as [OpenMOSS/claude-codex-handoff](https://github.com/OpenMOSS/claude-codex-handoff) solve asynchronous multi-agent coordination. This project solves lower-cost continuity between tasks with one replaceable file. It has no timers, queues, agent messaging, automatic tests, staging, commits, cloud sync, telemetry, or non-Git fallback.

Git is required for full evidence. Verification history is limited to evidence available in the current task. Secret lint is heuristic, not guaranteed detection. Submodule internals are opaque unless checkpointed separately. Cross-machine continuity requires the user to commit or otherwise transfer `HANDOFF.md`.

## Development

```bash
python -m unittest discover -s tests -v
python /path/to/skill-creator/scripts/quick_validate.py skills/project-checkpoint
```

Licensed under MIT.
