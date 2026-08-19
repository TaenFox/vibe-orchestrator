# Performance benchmark

Запуск из корня репозитория: `python benchmarks/performance/run_benchmark.py --project . --profile smoke --size small --storage sqlite --warmup 5 --iterations 30 --seed 35527 --output results/smoke.json`. Доступны размеры small/medium/large/xlarge (100/1000/5000/10000 tickets) и `--storage sqlite|yaml`; `--dataset` принимает ранее сохранённый JSON manifest и завершается ошибкой при неизвестной схеме. Harness копирует проект во временный каталог, поэтому исходный project и fixture не изменяются. `--cold` явно помечает cache eviction недоступным в worker и не используется для cold conclusion; warmup исключён из статистики.

Результат `performance-result.v1` содержит manifest, provenance, отдельный `case_id` для операций TicketStore, SessionStore, Scheduler и UI, raw samples и min/p50/p95/p99/max/mean/stdev. `wall_ms` обязателен; CPU и instrumented file counters дополняют его. HTTP/browser latency в этом worker-контексте не подтверждается.

Для CPU evidence: `python benchmarks/performance/profile.py --project <isolated-copy> --scenario scheduler.select_candidates --run-id <run_id> --manifest-hash <sha256> --output results/profile`. Pstats/text manifest связываются с run_id, case_id и checksum manifest.
