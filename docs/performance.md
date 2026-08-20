# Аудит производительности control plane

## Назначение и область аудита

Аудит измеряет production control plane без изменения его семантики: TicketStore и
SessionStore, scheduler, BudgetLedger, UI rendering и HTTP endpoints. Фикстуры
синтетические и анонимные; benchmark запускается на изолированной копии проекта,
а `source_checksum_before/after` проверяет отсутствие записи в исходное дерево.

## Functional baseline: UI/API, scheduler, persistence, budget lifecycle

Baseline включает `list/get/load_path/children_of` TicketStore; чтение, membership,
validation, overlap и lifecycle SessionStore; чтение, reservation lifecycle,
reconcile и error paths BudgetLedger; scheduler selection; UI board/fragment и
HTTP success/error endpoints. SQLite является runtime control plane. Режим `yaml`
использует `use_database=False` для tickets/sessions и сохраняет legacy YAML files;
ledger остаётся SQLite, поскольку это его authoritative persistence.
Варианты профиля материализуют small/medium/large/xlarge ticket sets и связанные
профильные counts сессий и ledger runs; manifest хранит фактические counts, а не
только поддерживаемые labels. Reservation/concurrency cases используют отдельные
synthetic budget IDs и удаляются после sample.

`validate_manifest(manifest, materialized_root=...)` — явная read-back граница для
изолированного проекта. Она перечитывает tickets, sessions и ledger, пересчитывает
counts, ticket status/run-history, session states и budget states и сравнивает их с
manifest. `generate_fixture` выполняет эту проверку перед возвратом; standalone
`load_dataset` без root сохраняет structural/logical-only проверку.

## Contract validation and completeness

Публикуемый `performance-result.v2` проверяется до `output.write_text()`. Validator
требует точную схему run metadata/parameters/manifest/registry, сверяет manifest с
его logical SHA-256 и materialized counts, проверяет полный уникальный registry для
режима storage, последовательность `sample_index == 0..iterations-1`, типы и
неотрицательность метрик, а также `min/p50/p95/p99/max/mean/stdev` по raw
`wall_ms`. `expected_outcome=success` означает, что каждый sample успешен
(`error=null`) и `errors=[]`. `expected_outcome=error` означает ошибку на каждой
итерации: каждый sample содержит непустую строку `error`, а `errors` содержит
ровно одну typed-запись (`sample_index`, `type`) для каждого sample. Typed evidence
обязана совпадать с `raw_samples[index].error`; смешение успешных и ошибочных
samples, отсутствие case, duplicate/out-of-range sample, malformed error или
silently ignored dataset делает run невалидным.

## Profiling artifacts and linkage

`profile-manifest.v1` связывает `run_id`, `case_id` и `dataset_manifest_hash`.
Каждый обязательный pstats/text/profile-manifest artifact описывается как
`{path, sha256, size_bytes, kind}`. Перед публикацией проверяются regular file,
безопасный относительный path внутри artifact root, размер и повторно вычисленный
SHA-256; profile evidence не входит в samples или iteration statistics.

`materialized_tree_sha256` хэширует authoritative stores: для SQLite —
`.vibe/control.sqlite3` и `.vibe/budgets/ledger.sqlite3`, для YAML — ticket/session
YAML и ledger SQLite. `-wal`/`-shm`, runtime и unrelated files исключаются.
Изменение статуса, удаление/добавление записи или mutation ledger приводит к
`ValueError` до принятия manifest.

## Warm/cold semantics and comparison eligibility

Warmup выполняется вне raw samples. В обычном режиме case имеет `mode=warm`. При
`--cold` mode `cold` разрешён только после успешной OS cache eviction capability;
при unavailable/failed preparation сохраняются limitation и descriptive evidence,
но cold conclusion запрещён. SQLite после полной валидации получает
`comparison_eligibility=eligible`; YAML остаётся допустимым legacy execution/archive
режимом с `historical_only` и не является обязательной comparison pair.

## States and errors

Фикстура материализует ticket statuses, draft/active/completed/cancelled sessions и
active/exhausted/blocked_unknown/over_budget budgets. В error cases проверяются
missing entities, malformed dataset, membership/validation failures, budget denial и
HTTP 4xx. В result ошибки ссылаются на конкретный `sample_index`.
Ожидаемый outcome — контракт всего case, а не требование наличия хотя бы одной
ошибки: success не допускает error evidence, а error не допускает успешных samples.
Связь profiling artifacts с `run_id`, `case_id` и `dataset_manifest_hash` остаётся
обязательной и проверяется независимо от outcome samples.

## Методика

CLI: `python3 benchmarks/performance/run_benchmark.py --project . --profile smoke
--size small --storage sqlite --warmup 5 --iterations 30 --seed 35527
--output /tmp/performance.json`. Доступны размеры `small=100`, `medium=1000`,
`large=5000`, `xlarge=10000`, а также `--dataset manifest.json`. Seed влияет на
порядок, статусы, parent/blocked связи и run histories. Warmup не попадает в raw
samples и агрегаты. `--cold` сообщает capability; если OS cache eviction недоступен,
samples помечены descriptive-only и не используются для cold conclusion.

Каждый case содержит стабильный `case_id`, component/operation/storage/dimensions,
raw timings, expected outcome, errors, sample count и aggregates. Mutation cases
используют заранее подготовленные IDs и idempotent lifecycle paths; исходный проект
не изменяется.

## Baseline results и hotspots

Numerical baseline создаётся только командой CLI и сохраняется в указанном JSON;
репозиторий не подменяет machine-specific timings. Выбранные profiling cases
создают pstats, text report и profile manifest, связанные по `run_id`, `case_id` и
manifest hash. Hotspot считается подтверждённым только при наличии такого artifact.

## Filesystem/SQLite attribution

`fs_ops`/`fs_bytes` — наблюдаемые deltas файлового дерева, не syscall trace. Для
SQLite instrumented benchmark connections собирают query/transaction/error counts,
lock wait time и `EXPLAIN QUERY PLAN`; для non-SQLite cases поля имеют `null` и
причину недоступности, а не zero. HTTP handler invocation и urllib network round
trip представлены отдельными cases.

## Ограничения и открытые решения

Browser DOM/focus/viewport/keyboard/auto-refresh не измеряются этим harness. OS-level
cache eviction и alternate filesystems capability-dependent; при недоступности
результат содержит причину и не формулирует portable comparison conclusion.
SQLite checksum — provenance текущей materialization и не обещает
кросс-машинную детерминированность binary layout. HTTP loopback может быть
недоступен в окружении; в таком случае producer должен сохранить limitation для
соответствующего case, поскольку validator не ослабляет outcome consistency.
