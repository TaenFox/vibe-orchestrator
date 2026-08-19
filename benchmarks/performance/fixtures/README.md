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
YAML tickets/sessions and records their tree checksum. одинаковые seed/profile/mode
дают одинаковый logical manifest/checksum, разные seed меняют generated order and
records. `--dataset` принимает только `performance-fixture.v2` manifest с полной
schema validation и возвращает typed `ValueError` для malformed/unsupported input.
