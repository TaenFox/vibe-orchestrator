# Аудит производительности control plane

## Назначение и область аудита

Аудит измеряет production control plane без изменения его семантики: TicketStore и
SessionStore, scheduler, BudgetLedger, UI rendering и HTTP endpoints. Benchmark
принимает synthetic fixture либо approved redacted dataset bundle (manifest JSON
рядом с материализованным `.vibe`); bundle копируется в изолированную копию и не
регенерируется. `source_checksum_before/after` проверяет отсутствие записи в
исходное дерево.

## Functional baseline: UI/API, scheduler, persistence, budget lifecycle

Baseline включает `list/get/load_path/children_of` TicketStore; чтение, membership,
validation, overlap и lifecycle SessionStore; чтение, reservation lifecycle,
reconcile и error paths BudgetLedger; scheduler selection; UI board/fragment и
HTTP success/error endpoints. SQLite является runtime control plane. Режим `yaml`
использует `use_database=False` для tickets/sessions и сохраняет legacy YAML files;
ledger остаётся SQLite, поскольку это его authoritative persistence.
Варианты профиля материализуют small/medium/large/xlarge ticket sets и связанные
профильные counts сессий и ledger runs; manifest хранит фактические counts, а не
только поддерживаемые labels. Для tickets действует точная cardinality-проверка:
`small=100`, `medium=1000`, `large=5000`, `xlarge=10000`, и `counts.tickets`
обязан совпадать с `SIZES[size]`. Reservation/concurrency cases используют отдельные
synthetic budget IDs и удаляются после sample.

## States and errors

Фикстура материализует ticket statuses, draft/active/completed/cancelled sessions и
active/exhausted/blocked_unknown/over_budget budgets. В error cases проверяются
missing entities, malformed dataset, membership/validation failures, budget denial и
HTTP 4xx. В result ошибки ссылаются на конкретный `sample_index`.

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
репозиторий не подменяет machine-specific timings. Top-сценарий каждого компонента
создаёт pstats, text report и единый profile manifest, связанные по `run_id`, case IDs
и manifest hash. Hotspot считается подтверждённым только при наличии такого artifact;
`--compare-storage` сохраняет измеренные aggregates для SQLite и legacy YAML.

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

## Fixture contract / dataset loading

Performance fixtures are synthetic audit data, not production interchange. The
baseline materializes tickets, delivery sessions and BudgetLedger runs for
`small`, `medium`, `large` and `xlarge`, including all declared ticket/session/
budget states and dimensions. SQLite is authoritative for the ledger in both
storage modes; YAML uses legacy ticket/session files.

The `performance-fixture.v2` manifest is strict: it records schema/source kind,
seed, storage, actual counts, dimensions, redaction policy, logical checksum and
materialized-tree checksum. The canonical logical checksum is SHA-256 of compact,
sorted-key JSON. `validate_manifest()` recomputes it and rejects missing fields,
unsupported values, profile counts (including exact ticket cardinality),
unmaterialized dimensions, non-synthetic IDs and tampered hashes. Consistent derived
dimensions and checksums do not make a manifest valid when `counts.tickets` differs
from the declared profile's `SIZES[size]`.

`dimensions.budget_states` is read back from every materialized ticket budget using
the effective status from the authoritative SQLite ledger. It contains every declared
state, uses non-negative integer counts, and its sum must equal `counts.tickets`.

`--dataset` is fail-closed. The supplied manifest is authoritative and is fully
validated before benchmark cases run. A manifest-only dataset may be materialized
deterministically into the isolated project using its validated seed/profile/storage;
the generated manifest's canonical logical payload and SHA-256 must equal the
supplied manifest before it can be used. `materialized_tree_sha256` is provenance
only and is excluded from this portable comparison. The result records
`dataset_materialization` and `dataset_manifest_hash` only after that proof. An
omitted `--storage` leaves the manifest's `storage_mode` authoritative; explicit
`--size`/`--storage` mismatches, malformed or incompatible manifests fail before
case construction and result output. No silent fallback to CLI defaults or
regeneration of another dataset is allowed; a mismatch raises `ValueError` and
the output JSON is not written.

Without `--dataset`, baseline generated mode remains controlled by CLI
seed/profile/storage. With `--dataset`, deterministic materialization is allowed
only as the documented manifest-only mode, and the isolated TicketStore,
SessionStore and BudgetLedger entities are used after identity verification.

The policy permits synthetic identifiers, counts, statuses and fixed timestamps
only. Non-empty titles, descriptions, prompts, raw payloads and production
identifiers are prohibited. Results carry dataset identity for every case and
retain equal source checksums before and after the run. Browser-level coverage is
unavailable in this worker context; cold-cache and materialized-tree checksums are
machine/filesystem dependent.

## Test/verification limitations

Fixture tests read back ticket IDs, statuses and run-history distributions,
session lifecycle states, and every authoritative SQLite ledger row (including
ticket ownership) for all four profiles and both storage modes. The ledger
read-back uses one ordered query so xlarge verification remains practical; budget
state snapshots continue to use `BudgetLedger.read_budget()`.
Browser DOM/focus/viewport/keyboard/auto-refresh checks are unavailable in the
worker environment and require an external or manual browser run; static tests
do not claim that coverage.
