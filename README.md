<div align="center">

<img src="assets/logo.png" width="144" alt="" />

# Project Checkpoint

**Portable, branch-aware continuity for AI coding tasks.**

[![CI](https://github.com/ted0103/ai-project-checkpoint/actions/workflows/ci.yml/badge.svg)](https://github.com/ted0103/ai-project-checkpoint/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/ted0103/ai-project-checkpoint?style=flat-square)](https://github.com/ted0103/ai-project-checkpoint/releases)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
[![MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

[Install](#install) · [Use](#use) · [How it works](#how-it-works) · [Resume states](#resume-states) · [Safety](#safety-and-limits)

</div>

Project Checkpoint is a portable [Agent Skill](skills/project-checkpoint/SKILL.md) for Git projects. It captures task state in `HANDOFF.md` and packages the actual working source into a verified ZIP that another AI can open without the original repository.

No daemon, cloud account, transcript archive, or runtime dependency beyond Python's standard library and Git.

Most handoff skills preserve what an agent said. Project Checkpoint also checks whether Git still agrees.

## Why use it?

AI tasks lose more than file changes when context ends. They also lose the goal, accepted decisions, delivered behavior, verification evidence, unresolved questions, and the precise next action.

Project Checkpoint preserves that operational context without copying the conversation:

- capability state: `built`, `in-progress`, or `blocked`, with evidence;
- active and superseded decisions, with reasons;
- acceptance criteria and observed verification;
- risks, open questions, and a small later remainder;
- one exact next action;
- branch, commit, working-tree manifest, and content-and-mode-sensitive fingerprint.
- local branch continuity or a portable, minimum-runnable working set for another AI.

| Capability | Plain handoff prompt | Project Checkpoint |
| --- | --- | --- |
| Preserve task context | Yes | Yes |
| Detect branch and commit drift | No | Yes |
| Detect dirty-file content or executable-mode drift | No | Yes |
| Detect later prose edits | No | Yes |
| Transfer the working source to another AI | No | Yes |
| Require a service or account | No | No |

## Install

Install the skill with the [Agent Skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add ted0103/ai-project-checkpoint --skill project-checkpoint
```

Or copy [`skills/project-checkpoint`](skills/project-checkpoint) into a skills directory supported by your agent.

> [!NOTE]
> The installer command requires Node.js/npm. The installed skill itself requires only Git and Python 3.10+ and runs on Windows, macOS, and Linux.

## Use

For another branch in the same app, tell your agent:

```text
checkpoint here
```

This writes the branch-aware `HANDOFF.md` and keeps the source local. For another AI without repository access, say:

```text
checkpoint else
```

This creates a verified minimum-runnable ZIP, extracts it into a temporary directory, and runs the smallest safe documented check from that copy. Upload the ZIP—not `HANDOFF.md` alone—to the other AI.

The current branch's project and questions are the default. If its scope is unclear, the skill keeps asking focused questions until you confirm its understanding. It always asks before including another discovered project; approved projects receive separate checkpoints so their files never get mixed.

In a new task, open the same project and say:

```text
resume project
```

The skill validates the checkpoint, explains how the current branch relates to the saved state, and returns the recovered context and exact next action. You can also invoke `$project-checkpoint` explicitly.

## How it works

### Checkpoint

1. Resolve one explicit Git worktree.
2. Record branch, commit, index state, working-tree mode, and bounded hashes of changed or untracked files.
3. Ask focused follow-ups until the current branch's questions and workstream are clear.
4. Ask before including every other discovered project; default to excluding it.
5. Show the final scope and wait for explicit user approval.
6. Reconcile only approved workstreams with repository and task evidence.
7. Sanitize, validate, and atomically replace the skill-owned `HANDOFF.md`.
8. Embed canonical metadata and a digest that detects later prose edits.
9. For `checkpoint here`, stop with the local handoff.
10. For `checkpoint else`, package and test the minimum-runnable working set.

### Portable bundle

The ZIP contains the selected working set plus:

- `.project-checkpoint/START_HERE.md` with concise resume instructions;
- `HANDOFF.md` with task and Git identity;
- `.project-checkpoint/bundle.json` with the selected profile and categories, canonical file hashes, sizes, modes, and archive identity.

The runnable profile includes active non-release progress, design/UI, runtime assets and vendor files, source, configuration and lockfiles, tests, documentation, data, and supporting files. Large design/reference assets, non-runtime vendor files, and releases stay out unless explicitly required. The ZIP also omits `.git`, Git history, deleted paths, and ignored files except explicit references. Bundle creation refuses stale handoffs, unsafe paths, submodule directories, likely secret files or credentials, and accidental overwrites.

Selected symlinks must resolve to files carried by the same profile, so a structurally verified bundle cannot silently contain an omitted target. UI-only bundles identify themselves as partial working sets rather than runnable projects.

### Resume

1. Validate the handoff structure, metadata, prose digest, and referenced files.
2. Compare the saved commit and working state with the current repository.
3. Classify the branch relationship and report bounded changed paths.
4. Return structured capabilities, decisions, acceptance criteria, verification, risks, questions, remainder, and next action.

## Resume states

| Status | Meaning |
| --- | --- |
| `fresh` | Exact saved branch, commit, and working state |
| `inherited` | Same commit and working state on another branch |
| `advanced` | Current commit descends from the checkpoint |
| `drifted` | Working state changed at the saved commit |
| `rewound` | Current commit is behind the checkpoint |
| `diverged` | Saved and current commits have different descendants |
| `unknown` | Git cannot inspect a required commit, or the repository is unborn |

Known commit relationships include ahead/behind counts and a bounded changed-path list. Verification recorded at checkpoint time is never presented as proof for later commits.

## Safety and limits

| Risk | Guardrail |
| --- | --- |
| Wrong project | Requires one explicit worktree; discovery is depth- and result-bounded |
| Ambiguous workstream | Asks focused follow-ups and requires approval of the final scope |
| Other discovered project | Excluded by default; explicit opt-in creates a separate checkpoint |
| Hand-edited checkpoint | Verifies generated prose and canonical hidden metadata |
| Conflicting bundle identity | Requires manifest branch, commit, and fingerprint to match `HANDOFF.md` |
| Unsafe references | Accepts repository-relative regular files; rejects symlinks and boundary escapes |
| Accidental overwrite | Refuses an unowned `HANDOFF.md` without approval for that exact path |
| Oversized AI transfer | Includes the runnable working set while excluding large reference assets and releases |
| Incomplete portable work | Verifies the ZIP, extracts it, and runs a documented safe check from the copy |
| Secret export | Excludes ignored files and rejects likely secret filenames, known token formats, and generic credential assignments |
| Context bloat | Caps the handoff at 120 lines, 1,000 words, and 24 KiB |
| Unbounded work | Caps individual files at 512 MiB and aggregate data at 2 GiB; caps archive entries at 20,000; rejects captured Git output over 16 MiB or inspection over 30 seconds |

> [!IMPORTANT]
> Repository state and handoff integrity are machine-checked. Agent-written narrative remains reconciled context, not certified truth. External facts may become stale, and secret detection is deliberately conservative. Verification proves archive self-consistency; it does not authenticate who created a ZIP or establish that included code is safe to execute. The digest detects edits but is not an adversarial signature.

> [!NOTE]
> Checkpointing never stages, commits, pushes, installs dependencies, deploys, messages agents, syncs to a service, or supports non-Git projects. `checkpoint else` may run an existing local, non-destructive check from the extracted ZIP. Portable ZIPs carry a working set, not Git history.

## Project scope

Project Checkpoint handles deliberate continuity between coding tasks. Tools such as [OpenMOSS/claude-codex-handoff](https://github.com/OpenMOSS/claude-codex-handoff) handle live asynchronous coordination, messaging, and process control. They solve different layers and can be used together.

The compact handoff keeps local resumes fast; the source ZIP makes cross-tool resumes self-contained.

## Development

The test suite covers structured context round-trips, reserved narrative text, schema-1 compatibility, branch inheritance, advanced/rewound/diverged histories, content and executable-mode drift, tampering, unsafe references, overwrite races, size and time ceilings, submodules, non-UTF-8 paths, Windows path behavior, portable profiles, omitted symlink targets, generic credential refusal, bounded metadata reads, internal identity agreement, corruption detection, and overwrite safety. CI runs Python 3.10 and 3.13 on Windows, macOS, and Ubuntu.

```bash
python -m unittest discover -s tests -v
python /path/to/skill-creator/scripts/quick_validate.py skills/project-checkpoint
```
