# vibe-orchestrator

Prototype of a pull-based, ticket-driven orchestration system for local Codex CLI agents.

The orchestrator owns **process rules and prompts**. The target project repository owns **tickets, code, knowledge and work artifacts**. Each agent run is a fresh `codex exec` invocation for one ticket at one stage.

## Current model

Three processes are included:

- **Discovery** — Idea → analysis → Investment Decision → implementation wait → validation.
- **Delivery** — Story / Task / Bug plus WIP-free Rework children.
- **Process Management** — Audit / Planning / Estimation.

Core rules:

- Pull from the **rightmost eligible queue** first, then corrective work, priority and age.
- Active stages have WIP limits.
- The orchestrator claims a ticket by moving it into the active stage **before** starting Codex.
- Rework/Correction child tickets are WIP-exempt; the parent remains blocked on its current stage and still occupies WIP.
- Agents return an `outcome`; they never edit workflow status themselves.

## Requirements

- Python 3.11+
- Git
- Codex CLI installed and authenticated (`codex --version`)
- VS Code recommended

On macOS, `/usr/bin/python3` or `python3` can still point to an older system Python that does not satisfy the `3.11+` requirement. Check your interpreter first:

```bash
python3 --version
```

If that prints a version older than `3.11`, install a newer Python and use that executable explicitly. Example with Homebrew:

```bash
brew install python@3.11
python3.11 --version
```

Codex is invoked with `codex exec --sandbox workspace-write --json --output-schema ... -o ... -`, using the CLI's existing authentication.

## Quick start in VS Code

```bash
git clone https://github.com/TaenFox/vibe-orchestrator.git
cd vibe-orchestrator
PYTHON_BIN="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)"
$PYTHON_BIN -c 'import sys; raise SystemExit("Python 3.11+ is required" if sys.version_info < (3, 11) else 0)'
$PYTHON_BIN -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Open this repository in VS Code. Included tasks provide setup, tests, orchestrator and UI commands.

Initialize a target Git repository:

```bash
vibe init /path/to/your-project
```

Add an idea:

```bash
vibe add /path/to/your-project discovery idea "My idea" \
  --description "What I want to explore"
```

Start the UI:

```bash
vibe ui /path/to/your-project
```

Open `http://127.0.0.1:8765`. Move a Discovery idea from **Todo** to **Ready**. `Ready` is the human-controlled commitment queue: the agent never pulls directly from Todo.

In a second VS Code terminal:

```bash
vibe run /path/to/your-project
```

The orchestrator polls tickets, respects WIP, and launches independent Codex subprocesses concurrently.

## Ticket storage

The target repository gets:

```text
.vibe/
├── README.md
├── .gitignore        # local agent run logs are ignored
└── tickets/
    ├── discovery/
    ├── delivery/
    └── process_management/
```

A ticket is a single YAML file. Example:

```yaml
id: DISC-A1B2C3
process: discovery
type: idea
title: Add family graph import
status: ready
priority: 100
parent: null
blocked_by: []
description: ...
wip_exempt: false
```

The prototype deliberately does **not** implement a database, Jira integration, users, permissions or a full event log.

## Process configuration

Processes are declarative YAML files in `workflows/`. Prompts live in `prompts/`. This is intentional for the prototype; later the prompt reader can be replaced with a versioned KMS provider without changing ticket/process mechanics.

## Safety

The default Codex sandbox is `workspace-write`, not `danger-full-access`. The orchestrator does not commit Codex auth files. Keep `.codex/auth.json` and other credentials outside project repositories.

This is an experimental local automation prototype. Run it only against repositories you can recover with Git.

## Known prototype limitations

- Human transitions are intentionally simple: UI buttons only follow the configured `next` transition.
- Investment Decision currently models the approve path; reject/correction buttons are a next iteration.
- Discovery-to-Delivery child creation and Implementation completion based on linked Delivery tickets are not automated yet.
- Failed Codex processes leave the parent ticket in its active stage so the failure remains visible in WIP.
- The UI is intentionally dependency-free and minimal.
