# Fixture contract

Фикстуры содержат только synthetic identifiers, counts, statuses и фиксированные
timestamps. titles, descriptions, prompts, raw payloads и production identifiers
запрещены (prohibited); `REDACTION_POLICY` и тесты проверяют это правило.

## Материализованные dimensions

`small=100`, `medium=1000`, `large=5000`, `xlarge=10000` tickets. Manifest содержит
фактические status counts, session states, budget states, `runs_per_ticket` 0/1/10,
session dimensions 1/10/100 и ledger dimensions 100/1000/10000. Для выбранного
профиля `counts` и `*_counts` отражают реально записанные сущности; labels не
считаются доказательством сами по себе.

## Форматы и determinism

SQLite materializes TicketStore/SessionStore databases; `yaml` materializes legacy
YAML tickets/sessions and records their authoritative-store checksum вместе с ledger
SQLite. Read-back пересчитывает counts/dimensions из фактических records, поэтому
labels в manifest не являются доказательством. одинаковые seed/profile/mode
дают одинаковый logical manifest/checksum, разные seed меняют generated order and
records. `--dataset` принимает только `performance-fixture.v2` manifest с полной
schema validation и возвращает typed `ValueError` для malformed/unsupported input.
Профильный `counts` в manifest равен реально записанным ticket/session/ledger
records; поддерживаемые dimensions вынесены отдельно и не выдаются за samples.
Изменение или удаление ticket/session/ledger record после генерации отвергается
по counts/dimensions либо `materialized_tree_sha256`; `control.sqlite3` и
`ledger.sqlite3` входят в checksum, transient `-wal`/`-shm` — нет.
