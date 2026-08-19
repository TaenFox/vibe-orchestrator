# Performance benchmark

Запуск из корня репозитория: `python benchmarks/performance/run_benchmark.py --project . --profile smoke --size small --storage sqlite --warmup 5 --iterations 30 --seed 35527 --output results/smoke.json`. Доступны размеры small/medium/large/xlarge (100/1000/5000/10000 tickets) и `--storage sqlite|yaml`; `--dataset` принимает ранее сохранённый JSON manifest и завершается ошибкой при неизвестной схеме. Harness копирует проект во временный каталог, поэтому исходный project и fixture не изменяются. `--cold` явно помечает cache eviction недоступным в worker и не используется для cold conclusion; warmup исключён из статистики.

Результат `performance-result.v2` содержит provenance, полный manifest, checksum исходного дерева, стабильный `case_id`, storage/dataset dimensions, raw samples, ошибки и min/p50/p95/p99/max/mean/stdev. Warmup не попадает в raw samples. `wall_ms` и `cpu_ms` измеряются всегда; filesystem counters — наблюдаемые file-count/byte deltas. SQLite query/lock поля остаются `null` только вместе с причиной недоступности instrumentation.

Registry включает TicketStore (`list`, `get`, `load_path`, `children_of`), SessionStore read/lifecycle/validation, BudgetLedger read/reserve/start/finalize/release/reconcile, scheduler, UI, HTTP handler и loopback transport success/error cases. Reservation использует заранее подготовленный idempotent run id.

`--storage yaml` materializes the same logical fixture through legacy TicketStore/SessionStore adapters; BudgetLedger remains SQLite control-plane storage. `--dataset` only accepts a validated `performance-fixture.v2` manifest and never silently falls back to synthetic data. `small/medium/large/xlarge` are 100/1000/5000/10000 tickets; declared dimensions include runs-per-ticket 0/1/10, session counts 1/10/100 and ledger counts 100/1000/10000.

Для CPU evidence: `python benchmarks/performance/profile.py --project <isolated-copy> --scenario scheduler.select_candidates --run-id <run_id> --manifest-hash <sha256> --output results/profile`. Pstats/text manifest связываются с run_id, case_id и checksum manifest; harness создаёт такую же связку для selected case при каждом запуске.
