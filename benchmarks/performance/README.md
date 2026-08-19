# Performance benchmark

Запуск из корня репозитория:

```bash
python3 benchmarks/performance/run_benchmark.py --project . --profile smoke \
  --size small --storage sqlite --warmup 5 --iterations 30 --seed 35527 \
  --output /tmp/performance-smoke.json
```

`--storage sqlite|yaml` выбирает control-plane storage; `--dataset` принимает
валидированный manifest bundle с материализованным `.vibe` и измеряет именно его,
не регенерируя synthetic fixture. Размеры: `small`, `medium`,
`large`, `xlarge`. `--cold` выполняет capability detection ОС, а unavailable
режим явно исключает cold conclusion. `--warm` — явная mutually-exclusive форма
обычного режима.

Result schema `performance-result.v2` содержит provenance, source checksums, полный
dataset manifest, case registry, raw samples, errors, aggregates, SQLite metrics и
profile links. `sample_count == len(raw_samples)`, warmup samples отсутствуют, а
ошибки ссылаются на существующий sample.

SQLite cases сохраняют query/transaction/error counters, lock timing и explain plans.
Concurrency registry включает отдельные idempotent и denied-overallocation cases;
результат фиксирует non-negative counters и terminal states. `sqlite_transaction_ms`
отделён от `sqlite_lock_ms` (lock wait; при отсутствии наблюдаемого wait значение
остаётся нулевым, а не подменяется длительностью transaction).
Profiling создаёт pstats/text для top-сценария каждого компонента и связывает их с
`run_id`, case IDs и manifest hash. `--compare-storage` дополнительно измеряет тот же
workload в alternate storage и сохраняет aggregate comparison. Смотрите
[каноническую методику](../../docs/performance.md).

Для `--storage yaml` tickets и sessions materialize legacy YAML documents через
`use_database=False`; BudgetLedger остаётся SQLite authoritative backend и явно
помечается в manifest. Если loopback bind запрещён окружением, HTTP cases всё равно
остаются в registry и получают limitation/error вместо исчезновения из результата.
