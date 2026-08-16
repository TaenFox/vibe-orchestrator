# Architecture

## Responsibility split

**Orchestrator repository**
- workflow definitions
- WIP/pull scheduling
- prompt provider (files for prototype)
- Codex worker launcher
- minimal UI

**Target project repository**
- `.vibe/tickets/**`
- source code
- project knowledge/docs
- implementation artifacts

**Future KMS**
- versioned prompts and policies exposed via API
- replace file prompt provider without changing scheduler/tickets

## Agent lifecycle

1. Human moves a ticket from backlog to an eligible queue (for example Discovery `ready`).
2. Scheduler scans all tickets and checks target-stage WIP.
3. The orchestrator changes the ticket to the active agent stage and sets `active_run`.
4. It starts an independent `codex exec` subprocess in the target repository.
5. Codex returns structured `{outcome, summary, details}`.
6. The orchestrator validates the outcome against workflow YAML and applies the configured transition.
7. The next queue can be pulled when its WIP allows.

The main orchestrator loop never waits synchronously for an agent: each Codex invocation is an asyncio subprocess task.

## Corrective work

`rework` and `correction` are tickets with `wip_exempt: true`. They may have a `parent`. The intended model is that the parent remains on the failed/control stage and continues consuming WIP while the corrective child is processed. Full automatic parent block/unblock wiring is intentionally deferred from v0.1.
