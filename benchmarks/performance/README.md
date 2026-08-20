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

SQLite cases сохраняют query/transaction/error counters, длительность завершённых
транзакций (`sqlite_transaction_ms` — сумма `BEGIN`–`COMMIT`/`ROLLBACK` в sample),
lock-wait fields и explain plans.
Concurrency registry включает отдельные idempotent и denied-overallocation cases;
результат фиксирует non-negative counters и terminal states. `sqlite_transaction_ms`
отделён от `sqlite_lock_wait_ms`/`sqlite_lock_wait_count`. Lock-wait fields имеют
значение `null`, когда ожидание не наблюдается инструментарием: stdlib `sqlite3`
не предоставляет portable busy-handler для измерения скрытого ожидания успешного
запроса. Они не подменяются длительностью транзакции.
Profiling обязательно создаёт coverage record для каждого компонента: `profiled`,
`unavailable` или `failed`; для `profiled` сохраняются pstats/text и manifest с
`run_id`, case IDs, warmup/iterations и manifest hash. Каждый smoke/full запуск
обязательно измеряет тот же workload в alternate storage и сохраняет raw samples,
aggregates и полный read-back proof; отдельного opt-in флага нет. Смотрите
[каноническую методику](../../docs/performance.md).

Standalone profiling поддерживает synthetic запуск без dataset (минимум —
`--project`, `--scenario`, `--output`) и approved запуск с обязательным точным
`--manifest-hash`. В обоих случаях создаётся полная coverage matrix; `--scenario`
фиксирует фокус, но не сокращает matrix. Synthetic пример:

```bash
python benchmarks/performance/profile.py --project . \
  --scenario ticketstore.list.delivery --output /tmp/performance-profile \
  --warmup 5 --iterations 30
```

Для approved dataset storage по умолчанию берётся из manifest:

```bash
python benchmarks/performance/profile.py --project . --dataset /path/to/approved \
  --scenario ticketstore.list.delivery --output /tmp/performance-profile \
  --run-id profile-smoke --manifest-hash <validated-hash> --warmup 5 --iterations 30
```

Smoke/full автоматически выполняют baseline и alternate storage. В результате
`storage_comparison` содержит оба manifest hash, logical/read-back equivalence и
индекс сопоставления `case_id`; `alternate_run` сохраняет второй набор samples.

Для `--storage yaml` tickets и sessions materialize legacy YAML documents через
`use_database=False`; BudgetLedger остаётся SQLite authoritative backend и явно
помечается в manifest. Если loopback bind запрещён окружением, HTTP cases всё равно
остаются в registry и получают limitation/error вместо исчезновения из результата.
