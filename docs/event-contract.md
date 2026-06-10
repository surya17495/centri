# CENTRI Event Contract

**Events are the source of truth; memory is a derived, re-derivable index.**

Every runtime event — emitted on the in-memory event bus and persisted to the
append-only SQLite ledger — follows one envelope. Memory, the hot context cache,
notifications, and any UI are all *projections* of this event stream. Drop them
and they can be rebuilt by replaying the ledger.

## Envelope

```json
{
  "type": "task.updated",
  "ts": "2026-06-01T12:00:00Z",
  "source": "jobs",
  "task_id": "tk-123",
  "thread_id": "th-123",
  "repo_id": "repo-123",
  "payload": {
    "status": "running",
    "summary": "Running tests"
  }
}
```

| Field        | Required | Notes                                                        |
|--------------|----------|--------------------------------------------------------------|
| `type`       | yes      | Dotted family name (see below).                              |
| `ts`         | yes      | ISO-8601 UTC timestamp.                                      |
| `source`     | yes      | Subsystem that emitted it (`coordinator`, `jobs`, `runtime`).|
| `task_id`    | no       | Correlates to a task row.                                    |
| `thread_id`  | no       | Correlates to a thread row.                                  |
| `repo_id`    | no       | Correlates to a repo row.                                    |
| `payload`    | yes      | Structured body; all new structured data goes here.         |

**Compatibility rule:** consumers may read top-level convenience fields during
migration, but new publishers put structured data in `payload`.

## Redaction (before persistence)

Because the ledger is append-only, a leaked secret would persist forever.
`centri.redaction` scrubs every event **before** it is written or fanned out:

- `db.append_event()` redacts `payload` before the `INSERT`.
- `event_bus.publish()` redacts the whole event before client fan-out.

Sensitive key *values* (`api_key`, `token`, `secret`, `password`, `authorization`,
`*_api_key`, …) become `[REDACTED]`; inline secrets (bearer headers, `KEY=...`
assignments, `sk-`/`ghp_`/`github_pat_`/`xox*` tokens, PEM private-key blocks)
are masked in free text.

## Required event families

| Family                       | Emitted by    |
|------------------------------|---------------|
| `user.utterance`             | coordinator   |
| `coordinator.response`       | coordinator   |
| `narrate`                    | coordinator   |
| `context.updated`            | coordinator   |
| `repo.changed`               | runtime/repo  |
| `approval.requested`         | coordinator   |
| `approval.resolved`          | coordinator/app |
| `task.started`               | coordinator   |
| `task.progress`              | jobs/hand     |
| `task.updated`               | jobs          |
| `task.completed`             | jobs          |
| `task.failed`                | jobs          |
| `task.cancelled`             | jobs          |
| `task.recovered`             | jobs          |
| `artifact.created`           | jobs          |
| `memory.synthesized`         | memory        |
| `notification.sent`          | runtime       |
| `hand.started`               | hand          |
| `hand.progress`              | hand          |
| `hand.blocked`               | hand          |
| `hand.completed`             | hand          |
| `hand.failed`                | hand          |

> `voice.*` families (`voice.session_started`, `voice.transcript_final`,
> `voice.speech_interrupted`) are reserved for Phase 3 and are **not** emitted in
> Phase 0.
