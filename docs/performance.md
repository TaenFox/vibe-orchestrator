# Performance audit

## 1. Назначение и область аудита

Документ фиксирует baseline локального control plane и процедуру повторного измерения UI/API, scheduler, TicketStore, SessionStore и BudgetLedger. Production behavior и форматы хранения не меняются. Browser-level DOM, focus, viewport, keyboard и фактическая задержка auto-refresh требуют внешнего/manual прогона.

## 2. Functional baseline: UI/API, scheduler, persistence, budget lifecycle

UI предоставляет `/`, `/fragment`, `/api/tickets`, `/api/sessions` и `/api/sessions/{id}`; board получает tickets workflow, применяет mode/search/status/active фильтры и рендерит HTML. Клиент запрашивает `/fragment` с интервалом 8 секунд. Scheduler выполняет полный scan, отбрасывает running/blocked/session/WIP/retry/dependency ограничения и сортирует кандидатов. В аудите handler rendering и urllib loopback round-trip являются разными case_id.

TicketStore и SessionStore в runtime используют `.vibe/control.sqlite3`; `use_database=False` оставлен для legacy YAML/migration comparison. TicketStore поддерживает get/list/load_path/children_of/save. SessionStore поддерживает list/get/load_path/save/create/activate/complete/cancel и membership validation. BudgetLedger хранит `.vibe/budgets/ledger.sqlite3`, а lifecycle reservation — `reserved_pending_start → started → finalized|released|unknown`; reconcile обрабатывает pending/unknown состояния.

## 3. States and errors

Измерительный fixture включает ready/todo, selected, active agent statuses, blocked и done, parents/dependencies, retry metadata, run_history 0/1/10, draft/active sessions и ledger runs. Missing ticket/session и исключения сохраняются в `errors`, malformed legacy files и lock/busy failures должны быть представлены отдельными cases при legacy/concurrency прогоне; unknown usage не трактуется как zero. Ошибки HTTP должны измеряться отдельными endpoint cases.

## 4. Методика

Команда: `python benchmarks/performance/run_benchmark.py --project . --profile smoke --size small --storage sqlite --warmup 5 --iterations 30 --seed 35527 --output results/DEL-355F27-smoke.json`; доступны размеры 100/1000/5000/10000 tickets и legacy YAML через `--storage yaml`. `--dataset` загружает и валидирует JSON manifest. Harness копирует project во временное isolated дерево и пишет только output. Warmup исключён; OS cache eviction в текущем worker недоступна, поэтому cold result содержит limitation и не используется для cold conclusion. Manifest содержит фактические counts, states, storage mode, redaction policy и SHA-256. Unavailable attribution остаётся `null` с причиной, а не zero.

## 5. Baseline results и hotspots

Числовой baseline генерируется командой и не подменяется неподтверждёнными цифрами в документации. Каждый result связан с `case_id`, `run_id`, git commit, source checksums и manifest hash. CPU evidence: `python benchmarks/performance/profile.py --project <isolated-copy> --scenario scheduler.select_candidates --run-id <run_id> --manifest-hash <sha256> --output results/profile`; profile manifest связывает `.pstats` и text report с запуском.

## 6. Filesystem/SQLite attribution

Результат отдельно фиксирует CPU/wall и instrumented file-count/byte deltas; это не syscall trace. SQLite-first и legacy YAML запускаются отдельными storage modes с одинаковыми logical dimensions и case IDs. Query/lock и alternate-filesystem attribution при недоступности capability помечаются причиной; portable conclusion не формулируется.

## 7. Optimization criteria

Рабочие SLO и thresholds требуют утверждения владельца. Предлагаемый gate: улучшение p95 не менее 20% считается существенным, regression p95 от 5% — поводом для расследования. Сравниваются одинаковые seed, manifest, case table, environment и cold/warm mode; повторяемость проверяется по counts/hashes и доверительному разбросу raw samples.

## 8. Ограничения и открытые решения

Текущий committed harness использует synthetic fallback: production-representative anonymized dataset, обязательные latency SLO, thresholds и разрешённые OS tools ещё не утверждены. Filesystem counters неполны без strace/dtruss/equivalent; browser-level проверки недоступны в worker-контексте. Запрещено сохранять titles/descriptions/prompts/raw sensitive payloads.
