# github/ai-project-checkpoint

**Say `checkpoint`. Resume from one compact, integrity-checked, branch-aware Markdown file.**

[Releases](https://github.com/ted0103/ai-project-checkpoint/releases) · Python 3.10+ · Windows, macOS, and Linux · MIT

Project Checkpoint is a portable Agent Skill for Git projects. It turns the useful state of an AI coding task into one bounded `HANDOFF.md`, then validates that handoff and explains how the current branch relates to it before another AI continues.

No daemon. No cloud account. No transcript dump. The runtime is Python’s standard library plus Git.

## Install

```bash
npx skills add ted0103/ai-project-checkpoint --skill project-checkpoint
```

Or copy [`skills/project-checkpoint`](skills/project-checkpoint) into any Agent Skills-compatible skills directory. The CLI installer requires Node.js/npm; the installed skill requires only Git and Python 3.10+.

## Use it

Inside a Git project:

```text
checkpoint
```

The skill resolves one explicit worktree and atomically writes an owned `HANDOFF.md` containing:

- the current goal and state;
- built, in-progress, and blocked capabilities with evidence;
- active or superseded decisions with reasons;
- acceptance criteria, observed verification, risks, and open questions;
- one exact next action and a small later remainder.

It records operational understanding, not every function or the conversation transcript.

In a fresh task:

```text
resume project
```

Resume validates the handoff, returns the complete structured context, and classifies the repository:

| Status | Meaning |
| --- | --- |
| `fresh` | Exact saved branch, commit, and working state |
| `inherited` | Same commit and working state on a newly created branch |
| `advanced` | Current commit descends from the checkpoint |
| `drifted` | Working state changed at the saved commit |
| `rewound` | Current commit is behind the checkpoint |
| `diverged` | Saved and current commits have different descendants |
| `unknown` | Git cannot prove the relationship, such as in a shallow clone |

For known commit relationships, resume reports ahead/behind counts and a bounded changed-path list. It never claims that earlier verification covers later commits.

You can also invoke `$project-checkpoint` explicitly.

## What it protects

| Risk | Guardrail |
| --- | --- |
| Resuming the wrong project | Requires one explicit Git worktree; discovery is bounded and never trusts an ancestor repository alone |
| Losing branch context | Classifies exact, inherited, advanced, rewound, and diverged histories with Git evidence |
| Forgetting delivered behavior | Carries a concise capability and acceptance ledger instead of a noisy symbol inventory |
| Losing discussion outcomes | Carries binding decisions, reasons, open questions, risks, and remainder |
| A hand-edited checkpoint | Verifies generated prose and canonical hidden metadata before resume |
| Unsafe file references | Accepts only repository-relative regular files; rejects symlinks and boundary escapes |
| Overwriting someone else’s handoff | Refuses an unowned `HANDOFF.md` without approval for that exact path |
| Context bloat | Caps the handoff at 120 lines, 1,000 words, and 24 KiB |

Git state and file integrity are machine-checked. Narrative truth is agent-reconciled context, so volatile facts outside the repository must be marked as potentially stale. Potential-secret detection is conservative and heuristic.

## Why one file

Tools such as [OpenMOSS/claude-codex-handoff](https://github.com/OpenMOSS/claude-codex-handoff) coordinate asynchronous agents. Project Checkpoint addresses a smaller problem: reliable continuity between coding tasks without operating another service.

The file is portable, reviewable, replaceable, and easy to commit or transfer with the project. Project Checkpoint does not run tests, stage changes, commit, push, message other agents, sync to a cloud, or support non-Git projects.

## Reliability

The test suite covers branch inheritance, descendant/rewound/diverged histories, structured context round-trips, fresh and dirty resumes, prose and metadata tampering, unsafe references, overwrite races, size ceilings, secret-shaped text, bounded hashing, submodules, non-UTF-8 paths, and Windows path behavior. CI runs on Windows, macOS, and Ubuntu with Python 3.10 and 3.13.

```bash
python -m unittest discover -s tests -v
python /path/to/skill-creator/scripts/quick_validate.py skills/project-checkpoint
```

Licensed under [MIT](LICENSE).
