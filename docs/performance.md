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
только поддерживаемые labels. Reservation/concurrency cases используют отдельные
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
