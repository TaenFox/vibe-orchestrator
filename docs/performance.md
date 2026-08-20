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
репозиторий не подменяет machine-specific timings. Выбранные profiling cases
создают pstats, text report и profile manifest, связанные по `run_id`, `case_id` и
manifest hash. Hotspot считается подтверждённым только при наличии такого artifact.

## SQLite attribution schema

`fs_ops`/`fs_bytes` — наблюдаемые deltas файлового дерева, не syscall trace. Для
SQLite cases connection factory устанавливается только harness-ом и собирает
`sqlite_queries`, `sqlite_transactions`, `sqlite_errors` и
`sqlite_busy_errors` per sample. `sqlite_attribution` содержит источник
instrumentation и версию контракта. Для SQLite SQL cases benchmark сохраняет
`sqlite_explain_query_plan` на уровне конкретного `case_id`: TicketStore покрывает
process/full list и `ticket_id` lookup, SessionStore — list/get и case-specific
lifecycle read families (ticket validation и existing-session lookup, где они
выполняются). Validation-error cases получают только query families, достигнутые
до ошибки; например, membership/overlap validation получает `tickets.ticket_id`
lookup без нерелевантного `sessions` lookup. Записи lifecycle в `sessions`, `session_members` и `events` не
подменяются выдуманными DML plans; case содержит typed limitation о том, что
`EXPLAIN QUERY PLAN` не даёт portable write-plan contract.
Это статическое audit evidence формы запроса, а не trace фактически выполненных
statements. TicketStore, SessionStore и BudgetLedger используют один factory,
поэтому counters не смешиваются между компонентами или итерациями. Для
YAML/non-SQLite cases и SQLite-backed `load_path` SQLite fields равны `null`, а
`sqlite_attribution` содержит limitation; `load_path` читает YAML и корректно имеет
пустой plan. Остальные SQLite cases получают attribution и планы только для
фактически используемых SQL query families.

## Lock/busy wait methodology

`sqlite_lock_wait_ms` и `sqlite_lock_wait_count` — отдельные поля для
подтверждённого ожидания writer lock. Обычная длительность `BEGIN`–`COMMIT`
туда не попадает. Текущая реализация сохраняет стандартные `timeout` и
`busy_timeout` и классифицирует текстовые `SQLITE_BUSY`/`SQLITE_LOCKED`
ошибки в `sqlite_busy_errors`, но stdlib `sqlite3` не предоставляет portable
busy-handler callback для измерения скрытого ожидания успешного запроса.
Поэтому lock-wait fields сериализуются как `null` с явной limitation; это не
означает нулевое ожидание и не меняет production retry/timeout semantics.

## Concurrency and invariants matrix

Regression tests используют отдельные SQLite databases и ThreadPoolExecutor:

| Сценарий | Проверяемый инвариант |
| --- | --- |
| identical `reserve(run_id)` | одна run row и один aggregate increment |
| distinct reservations | tokens/points/runs не превышают limits |
| ticket + session admission | denial не оставляет partial run или aggregate |
| concurrent terminal transition | state, terminal metadata и usage изменяются один раз |
| unknown/adjustment | unknown сохраняет существующее blocking rule; adjustment append-only и atomic |

Операционные ошибки, ожидаемые case-сценарием, остаются в raw sample `error`
и одновременно учитываются как SQLite errors, если это `sqlite3.Error`.

## Expected errors and result validation

`validate_result` требует ссылки ошибок на существующие sample indices и при
наличии SQLite counters проверяет полный attribution набор, согласованность
lock-wait fields и непустой source. Если case содержит explain plans, проверяются
строковые `query` и `detail: list[str]`; plans сериализуются в JSON без
SQLite connection/cursor объектов. Схема `performance-result.v2` остаётся
backward-compatible: старые artifacts без новых optional fields принимаются,
новые instrumented samples обязаны содержать их. `BudgetDenied`,
`ImmutableRunError` и validation errors остаются частью case outcome.

## Execution commands

```text
python -m pytest --collect-only -q
python -m pytest tests/test_budget_ledger.py tests/test_performance_benchmark.py -q
python -m pytest tests/test_control_db.py tests/test_db_primary_store.py tests/test_run_store_runtime.py -q
python benchmarks/performance/run_benchmark.py --project <isolated-project> --profile smoke --size small --storage sqlite --warmup 1 --iterations 3 --seed 35527 --output /tmp/performance.json
```

## Limitations and open decisions

Browser DOM/focus/viewport/keyboard/auto-refresh не измеряются этим harness. OS-level
cache eviction и alternate filesystems capability-dependent; при недоступности
результат содержит причину и не формулирует portable comparison conclusion.
Портативного точного busy-handler wait metric в Python 3.11 нет; для заполнения
lock-wait fields потребуется explicit retry wrapper или platform-specific tracing.
EXPLAIN зависит от версии SQLite и индексов; для lifecycle DML фиксируются
только фактически выполняемые связанные read/query families, поскольку explain
detail для записи не является универсальным контрактом, а limitation сохраняет
это различие явным. Сейчас выбран benchmark-only connection injection;
production connection factory по умолчанию не меняется.
