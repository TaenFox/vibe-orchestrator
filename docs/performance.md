# Аудит производительности control plane

## Назначение и область аудита

Аудит измеряет production control plane без изменения его семантики: TicketStore и
SessionStore, scheduler, BudgetLedger, UI rendering и HTTP endpoints. Фикстуры
синтетические и анонимные; benchmark запускается на изолированной копии проекта,
а `source_checksum_before/after` проверяет отсутствие записи в исходное дерево.

## Functional baseline: UI/API, scheduler, persistence, budget lifecycle

Baseline включает `list/get/load_path/children_of/is_done/run_path` и write paths
TicketStore; чтение, membership, agent membership, validation, overlap и lifecycle
SessionStore; чтение, reservation/decision lifecycle, reconcile и error paths
BudgetLedger; scheduler selection/WIP count; UI board/fragment/drawer и HTTP
success/error endpoints. SQLite является runtime control plane. Режим `yaml`
использует `use_database=False` для tickets/sessions и сохраняет legacy YAML files;
ledger остаётся SQLite, поскольку это его authoritative persistence.
Варианты профиля материализуют small/medium/large/xlarge ticket sets и связанные
профильные counts сессий и ledger runs; manifest хранит фактические counts, а не
только поддерживаемые labels. Reservation/concurrency cases используют отдельные
synthetic budget IDs и удаляются после каждого sample.

## States and errors

Фикстура материализует ticket statuses, draft/active/completed/cancelled sessions и
active/exhausted/blocked_unknown/over_budget budgets. В error cases проверяются
missing entities, malformed dataset, membership/validation failures, budget denial и
HTTP 4xx. В result ошибки ссылаются на конкретный `sample_index`.

## Изменения DEL-355F27: registry и isolation

Каждый registry item — именованный `CaseSpec` с `case_id`, component, operation,
`kind` (`read_only` или `mutation`), `expected_outcome`, `storage_modes` и
callbacks setup/run/teardown. Старый tuple-доступ сохранён для совместимых
потребителей. Успешные и ожидаемо ошибочные публичные операции представлены
отдельными cases; недоступный transport получает `limitations`, а не исчезает.

Перед каждым sample harness снимает normalized logical snapshot control-plane
SQLite/YAML entities и paths. Нормализация исключает только явно перечисленные
volatile timestamp fields. Mutation callbacks владеют synthetic IDs и удаляют
child rows/events до parent rows в `finally`. Результат содержит `isolation` с
`before_hash`, `after_hash`, `leaked_entities`, `leaked_paths`, `cleanup_errors`
и `clean`; mutation без evidence или с `clean=false` отклоняется
`validate_result()`. Ожидаемая exception остаётся в `errors` с `sample_index` и
не является загрязнением при чистом snapshot.

## Методика

CLI: `python3 benchmarks/performance/run_benchmark.py --project . --profile smoke
--size small --storage sqlite --warmup 5 --iterations 30 --seed 35527
--output <output-dir>/performance.json`. Доступны размеры `small=100`, `medium=1000`,
`large=5000`, `xlarge=10000`, а также `--dataset manifest.json`. Seed влияет на
порядок, статусы, parent/blocked связи и run histories. Warmup не попадает в raw
samples и агрегаты. `--cold` сообщает capability; если OS cache eviction недоступен,
samples помечены descriptive-only и не используются для cold conclusion.

Каждый case содержит стабильный `case_id`, component/operation/kind/storage
applicability/dimensions, raw timings, expected outcome, errors, sample count,
aggregates и isolation evidence. Warmup проходит callback, но не попадает в
samples; исходный проект не изменяется.

## Baseline results и hotspots

Numerical baseline сохраняется в committed artifact
`benchmarks/performance/artifacts/baseline-small-seed-35527.json`, а CLI остаётся
источником machine-specific повторных измерений. Выбранные profiling cases
создают pstats, text report и profile manifest, связанные по `run_id`, `case_id` и
manifest hash. Hotspot считается подтверждённым только при наличии такого artifact.
Committed baseline использует synthetic-only fixture (`seed=35527`, `warmup=5`,
`iterations=30`); warmup samples excluded, and every available case retains 30
raw samples with p50/p95 recalculated from those samples. Result and profile
manifest share a stable run ID and manifest hash; baseline descriptors carry
relative paths, checksums, sizes and command hashes for every committed profile
artifact. For the committed result, descriptor paths resolve from the canonical
base `benchmarks/performance/artifacts/`; this baseline therefore uses
`profile-small-seed-35527/<file>`. Standalone `profile.py` keeps paths relative
to its `--output` directory.

## Approved dataset и storage comparison

`--dataset` принимает directory bundle с материализованным `.vibe` и
`manifest.json` либо путь к manifest внутри такого bundle. В approved режиме
harness копирует фактическое дерево через `materialize_dataset()` и не вызывает
`generate_fixture()`. Проверяются schema/version, manifest hash, tree checksum,
storage identity и read-back proof; несовместимый bundle завершается ошибкой до
измерения. Result сохраняет `source_kind=approved_dataset`, provenance,
`dataset_manifest_hash`, `dataset_tree_sha256` и измеренный manifest.

Каждый smoke/full запуск автоматически выполняет два изолированных прохода:
`--storage` — baseline, противоположный режим — alternate. Поле `cases`
сохраняет совместимый baseline result, `alternate_run` содержит второй проход,
а `storage_comparison` сопоставляет case IDs и logical/read-back proof. Cases,
недоступные в alternate режиме, остаются в registry с явным limitation.

## Standalone profiling и coverage manifest

Standalone profiling допускает synthetic команду с `--project`, `--scenario` и
`--output`; approved dataset требует точного `--manifest-hash`. Обязательная
матрица централизована в benchmark registry и включает TicketStore, SessionStore,
BudgetLedger, Orchestrator, Scheduler, UI и HTTP. Для каждой записи сохраняется
`profiled`, `failed` или `unavailable`, sample parameters, errors и descriptors
отдельных pstats/text artifacts в финальном `profile-manifest.json`. Requested
scenario валидируется и сохраняется как focus, но не отключает остальные записи.

## Artifact path and integrity rules

When an artifact root is supplied, `validate_result()` resolves every descriptor
from that explicit base, never from the current working directory. Absolute
paths, `..`, paths escaping the root, missing files, directories and symlinks
are invalid. pstats/text files must be regular files whose `size_bytes` and
SHA-256 match the descriptor. `profile-manifest.json` must resolve to a regular
file, but its checksum is not compared because the manifest contains its own
descriptor and a recursive checksum would be impossible. Standalone output-
relative paths remain unchanged.

## SQLite attribution schema

`fs_ops`/`fs_bytes` — наблюдаемые deltas файлового дерева, не syscall trace. Для
SQLite cases connection factory устанавливается только harness-ом и собирает
`sqlite_queries`, `sqlite_transactions`, `sqlite_errors` и
`sqlite_busy_errors` per sample. `sqlite_transaction_ms` — сумма длительностей
завершённых `BEGIN`–`COMMIT`/`ROLLBACK` транзакций, измеренная trace callback.
`sqlite_attribution` содержит источник
instrumentation и версию контракта; `sqlite_explain_query_plan` остаётся
привязанным к case. TicketStore, SessionStore и BudgetLedger используют один
factory, поэтому counters не смешиваются между компонентами или итерациями.
Для YAML/non-SQLite cases SQLite fields равны `null`, а
`sqlite_attribution` содержит limitation, а не ложные нули.

## Lock/busy wait methodology

`sqlite_lock_wait_ms` и `sqlite_lock_wait_count` — отдельные поля для
подтверждённого ожидания writer lock. Обычная длительность `BEGIN`–`COMMIT`
туда не попадает. Текущая реализация сохраняет стандартные `timeout` и
`busy_timeout` и классифицирует текстовые `SQLITE_BUSY`/`SQLITE_LOCKED`
ошибки в `sqlite_busy_errors`, но stdlib `sqlite3` не предоставляет portable
busy-handler callback для измерения скрытого ожидания успешного запроса.
Поэтому lock-wait fields сериализуются как `null` с явной limitation; это не
означает нулевое ожидание и не меняет production retry/timeout semantics.

## Изменения DEL-5D2004: UI render path

Full board и `/fragment` используют request-local read model: каждый `budget_id`
получает не более одного snapshot и списка runs, а delivery sessions читаются один
раз и индексируются по ticket ID. Полная страница рендерит только drawer shell;
выбранная панель загружается свежим `/drawer`, сохраняя context, budget, retry,
session/tree, run history и artifact links. `/fragment` по-прежнему возвращает
только board.

The drawer lifecycle keeps this lazy contract across repeated use: a successful
load may replace the shell with a fetched panel, while every close removes that
panel and restores one reusable loading shell. Close via button, Escape, or backdrop
also restores focus to the opener when it remains connected; a removed opener is a
safe no-op. A request token prevents a response arriving after close from reopening
the drawer or inserting stale content. The Node harness covers these unit-level
transitions; real browser DOM/focus/keyboard/viewport smoke remains unavailable in
the worker and requires an external or manual run.

Regression cases в `tests/test_ui.py` проверяют call counts, lazy markup, фильтры,
fresh drawer endpoint, escaping/API payloads и существующие auto-refresh guards.
Node lifecycle harness также проверяет два последовательных открытия, восстановление
shell и focus для close button/backdrop, а также игнорирование late response.
Benchmark registry сохраняет board/fragment/drawer HTTP cases и сравнение выполняется
при одинаковых dataset, storage, seed, warmup и iterations. Browser-level DOM/focus/
viewport, Escape и Tab smoke в worker недоступен; telemetry и абсолютный SLO отсутствуют.

## Optimization criteria and regression policy

**Decision status (DEL-355F27): no-SLO.** The owner decision is recorded in
[`docs/decisions/DEL-355F27-performance-policy.md`](decisions/DEL-355F27-performance-policy.md),
which contains the authority, decision timestamp, scope, rationale and stable
identifier. No absolute latency SLO has been approved. This baseline is
descriptive-only and must not be interpreted as an SLO pass/fail result.

Regardless of the no-SLO decision, an improvement of at least 20% in the agreed metric for
the same case, dataset, storage mode and runtime conditions is a significant
optimization candidate. A degradation greater than 5% is a regression signal
and requires investigation.

Comparisons use the same seed, manifest, case set, storage and warmup/iteration
parameters. The comparison is repeated in a fresh isolated checkout; p50 and
p95 are preferred, while stdev and raw samples are used to assess variance and
outliers. A single outlier does not establish a regression when a repeat run
does not reproduce it, but repeated >5% degradation is escalated. Unavailable
cases remain unavailable and are excluded from claims rather than treated as
zero latency. Filesystem values are instrumented file-count/bytes deltas, not
syscall traces; lock-wait remains `null` because stdlib `sqlite3` lacks a
portable busy handler; browser/DOM/viewport/keyboard/auto-refresh checks are
outside this worker's capabilities.

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
lock-wait fields и непустой source. Схема `performance-result.v2` остаётся
backward-compatible: старые artifacts без новых optional fields принимаются,
новые instrumented samples обязаны содержать их. `BudgetDenied`,
`ImmutableRunError` и validation errors остаются частью case outcome.

## Execution commands

```text
python -m pytest --collect-only -q
python -m pytest tests/test_budget_ledger.py tests/test_performance_benchmark.py -q
python -m pytest tests/test_control_db.py tests/test_db_primary_store.py tests/test_run_store_runtime.py -q
python benchmarks/performance/run_benchmark.py --project <isolated-project> --profile smoke --size small --storage sqlite --warmup 5 --iterations 30 --seed 35527 --output <output-dir>/performance.json
```

## Limitations and open decisions

- The DEL-355F27 no-SLO decision is closed by the committed decision record
  above; absolute latency targets remain intentionally unapproved.

Browser DOM/focus/viewport/keyboard/auto-refresh не измеряются этим harness. OS-level
cache eviction и alternate filesystems capability-dependent; при недоступности
результат содержит причину и не формулирует portable comparison conclusion.
HTTP loopback может быть запрещён окружением, а YAML registry не запускает
network-level routes; оба ограничения записываются в `limitations`. Статические
render/handler тесты не являются browser-level проверкой.
Портативного точного busy-handler wait metric в Python 3.11 нет; для заполнения
lock-wait fields потребуется explicit retry wrapper или platform-specific tracing.
Сейчас выбран benchmark-only connection injection; production connection factory
по умолчанию не меняется.
