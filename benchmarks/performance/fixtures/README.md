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
Профильный `counts` в manifest равен реально записанным ticket/session/ledger
records; поддерживаемые dimensions вынесены отдельно и не выдаются за samples.

## Manifest schema and loading contract

`performance-fixture.v2` обязателен и содержит `schema_version`, `source_kind`,
`seed`, `storage_mode`, `counts`, `dimensions`, `hashes`,
`fixture_files_sha256`, `logical_checksum`, `materialized_tree_sha256` и
`redaction_policy`. Canonical logical payload сериализуется JSON с
`sort_keys=True` и compact separators и хэшируется SHA-256. `hashes.manifest_sha256`,
`logical_checksum` и `fixture_files_sha256` должны совпадать с этим пересчётом;
`materialized_tree_sha256` хранится отдельно как provenance и не является
portable identity SQLite.

`load_dataset()` принимает JSON manifest-only dataset только после полной проверки
схемы, counts, states, dimensions, synthetic IDs и checksum. При `--dataset`
manifest является authoritative source для seed/profile/storage; несовпадение
явно заданных `--size` или `--storage`, повреждение manifest и unsupported values
останавливают benchmark до выполнения case и до записи результата. После проверки
разрешена только документированная deterministic materialization в isolated project;
каноническая logical payload и `manifest_sha256` materialization должны совпасть с
входным manifest. Только после этого результат и cases получают подтверждённую
identity входного dataset; при mismatch выбрасывается `ValueError` до `_cases()` и
`output.write_text()`, а fallback к CLI/default synthetic dataset запрещён.

Baseline без `--dataset` сохраняет генерацию по CLI seed/profile/storage. Read-back
проверки fixture перечитывают реальные tickets, sessions и ledger runs для каждого
профиля small/medium/large/xlarge, а не только manifest counts.

## Ограничения

`materialized_tree_sha256` может зависеть от SQLite layout и lifecycle metadata;
для воспроизводимой identity используется logical checksum. Browser-level
DOM/focus/viewport/keyboard/auto-refresh проверки этим worker-окружением не
выполняются. Cold-cache capability также зависит от машины.
