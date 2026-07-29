# github/ai-project-checkpoint

**Say `checkpoint`. Resume from one compact, integrity-checked Markdown file.**

[v0.1.0](https://github.com/ted0103/ai-project-checkpoint/releases/tag/v0.1.0) · Python 3.10+ · Windows, macOS, and Linux · MIT

Project Checkpoint is a portable Agent Skill for Git projects. It turns the useful state of an AI coding task into one bounded `HANDOFF.md`, then checks that handoff against the repository before another task resumes.

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

The skill resolves one explicit worktree, inspects its Git state, reconciles the task narrative with observed evidence, and atomically replaces its own `HANDOFF.md`.

In a fresh task:

```text
resume project
```

The skill validates the handoff and current repository state before returning only the recovered goal, current condition, and exact next action. If the repository changed, it reports evidence-supported drift instead of pretending the checkpoint is fresh.

You can also invoke `$project-checkpoint` explicitly.

## What it protects

| Risk | Guardrail |
| --- | --- |
| Resuming the wrong project | Requires one explicit Git worktree; discovery is bounded and never trusts an ancestor repository alone |
| Stale repository context | Records the branch, commit, bounded manifest, and a content-sensitive Git fingerprint |
| A hand-edited checkpoint | Verifies the generated prose digest and hidden metadata before resume |
| Unsafe file references | Accepts only repository-relative regular files; rejects symlinks and boundary escapes |
| Overwriting someone else’s handoff | Refuses an unowned `HANDOFF.md` without approval for that exact path |
| Context bloat | Caps the handoff at 120 lines, 1,000 words, and 24 KiB |

Checkpointing records Git identity, current state, decisions, observed verification, risks, and one executable next action. Narrative claims remain agent-reconciled context—not machine-certified truth. Potential-secret detection is deliberately conservative and heuristic.

## Why one file

Tools such as [OpenMOSS/claude-codex-handoff](https://github.com/OpenMOSS/claude-codex-handoff) coordinate asynchronous agents. Project Checkpoint addresses a smaller problem: reliable continuity between coding tasks without operating another service.

The file is portable, reviewable, replaceable, and easy to commit or transfer with the project. Project Checkpoint does not run tests, stage changes, commit, push, message other agents, sync to a cloud, or support non-Git projects.

## Reliability

The test suite covers fresh and drifted resumes, prose and metadata tampering, unsafe references, overwrite races, size ceilings, secret-shaped text, bounded hashing, submodules, non-UTF-8 paths, and Windows path behavior. CI runs on Windows, macOS, and Ubuntu with Python 3.10 and 3.13.

```bash
python -m unittest discover -s tests -v
python /path/to/skill-creator/scripts/quick_validate.py skills/project-checkpoint
```

Licensed under [MIT](LICENSE).
