# Performance benchmark

Запуск из корня репозитория: `python benchmarks/performance/run_benchmark.py --project . --profile smoke --warmup 5 --iterations 30 --seed 35527 --output results/smoke.json`. Harness копирует проект во временный каталог, поэтому исходный project и fixture не изменяются. `--cold` использует 100 samples для noisy filesystem cases; warmup исключён из статистики.

Результат `performance-result.v1` содержит manifest, provenance, отдельный `case_id` для операций TicketStore, SessionStore, Scheduler и UI, raw samples и min/p50/p95/p99/max/mean/stdev. `wall_ms` обязателен; CPU и instrumented file counters дополняют его. HTTP/browser latency в этом worker-контексте не подтверждается.

Для CPU evidence: `python benchmarks/performance/profile.py --project <isolated-copy> --scenario scheduler.select_candidates --output results/profile`. Pstats связывается с тем же `case_id`.
