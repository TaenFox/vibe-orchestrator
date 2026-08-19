# Performance audit

## 1. Назначение и область аудита

Документ фиксирует baseline локального control plane и процедуру повторного измерения UI/API, scheduler, TicketStore, SessionStore и BudgetLedger. Production behavior и форматы хранения не меняются. Browser-level DOM, focus, viewport, keyboard и фактическая задержка auto-refresh требуют внешнего/manual прогона.

## 2. Functional baseline: UI/API, scheduler, persistence, budget lifecycle

UI предоставляет `/`, `/fragment`, `/api/tickets`, `/api/sessions` и `/api/sessions/{id}`; board получает tickets workflow, применяет mode/search/status/active фильтры и рендерит HTML. Клиент запрашивает `/fragment` с интервалом 8 секунд. Scheduler выполняет полный scan, отбрасывает running/blocked/session/WIP/retry/dependency ограничения и сортирует кандидатов.

TicketStore и SessionStore в runtime используют `.vibe/control.sqlite3`; `use_database=False` оставлен для legacy YAML/migration comparison. TicketStore поддерживает get/list/load_path/children_of/save. SessionStore поддерживает list/get/load_path/save/create/activate/complete/cancel и membership validation. BudgetLedger хранит `.vibe/budgets/ledger.sqlite3`, а lifecycle reservation — `reserved_pending_start → started → finalized|released|unknown`; reconcile обрабатывает pending/unknown состояния.

## 3. States and errors

Измерительный fixture включает ready/todo, selected, active agent statuses, blocked и done, parents/dependencies, retry metadata, run_history 0/1/10, draft/active sessions и ledger runs. Missing ticket/session и исключения сохраняются в `errors`, malformed legacy files и lock/busy failures должны быть представлены отдельными cases при legacy/concurrency прогоне; unknown usage не трактуется как zero. Ошибки HTTP должны измеряться отдельными endpoint cases.

## 4. Методика

Команда: `python benchmarks/performance/run_benchmark.py --project . --profile smoke --warmup 5 --iterations 30 --seed 35527 --output results/DEL-355F27-smoke.json`; full использует medium fixture. Harness копирует project во временное isolated дерево, передаёт стабильный seed и пишет только output. `perf_counter_ns` даёт wall milliseconds, `process_time_ns` — CPU milliseconds. Warmup исключён; cold filesystem cases используют 100 samples. Manifest содержит counts, dimensions, redaction policy и SHA-256. Raw sample schema: `sample_index`, `wall_ms`, `cpu_ms`, `fs_ops`, `fs_bytes`, `sqlite_queries`, `sqlite_lock_ms`, `error`. Aggregates: min/p50/p95/p99/max/mean/stdev.

## 5. Baseline results и hotspots

Числовой baseline генерируется командой и не подменяется неподтверждёнными цифрами в документации. Каждый result связан с `case_id`, `run_id`, git commit, source checksums и manifest hash. CPU evidence: `python benchmarks/performance/profile.py --project <isolated-copy> --scenario scheduler.select_candidates --output results/profile`; артефакты `.pstats` и text report указываются в `profiling.artifacts` при внешнем запуске.

## 6. Filesystem/SQLite attribution

Результат отдельно фиксирует CPU/wall и instrumented file-count/byte deltas; это не syscall trace. SQLite query/lock counters требуют доступного wrapper/profiler и иначе помечаются `null` с limitation. Runtime baseline SQLite-first; legacy YAML должен запускаться отдельным dataset/storage mode. Без второй согласованной filesystem машины comparison остаётся limitation, а не выводом о переносимой производительности.

## 7. Optimization criteria

Рабочие SLO и thresholds требуют утверждения владельца. Предлагаемый gate: улучшение p95 не менее 20% считается существенным, regression p95 от 5% — поводом для расследования. Сравниваются одинаковые seed, manifest, case table, environment и cold/warm mode; повторяемость проверяется по counts/hashes и доверительному разбросу raw samples.

## 8. Ограничения и открытые решения

Текущий committed harness использует synthetic fallback: production-representative anonymized dataset, обязательные latency SLO, thresholds и разрешённые OS tools ещё не утверждены. Filesystem counters неполны без strace/dtruss/equivalent; browser-level проверки недоступны в worker-контексте. Запрещено сохранять titles/descriptions/prompts/raw sensitive payloads.
