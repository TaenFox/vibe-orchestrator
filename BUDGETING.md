# Версионируемый контракт budget control plane

Статус: принятый контракт `budget.v1`; runtime enforcement в текущем MVP не
реализован. Этот документ фиксирует модель и границы, которые должны быть
соблюдены при последующей реализации Delivery.

## Назначение и границы

Budget control plane ограничивает нормализованное ресурсное потребление
агентными запусками Delivery. В контракт входят token- и point-измерения,
лимит количества запусков, резервирование до старта, подтверждение факта после
завершения, traceability и аудит корректировок.

В контракт не входят Discovery и process_management budgets, денежный биллинг,
фактические цены провайдера, остановка уже запущенного Codex, изменение WIP,
retry-backoff, workflow-статусов и UI/CLI авторизация. Эти решения могут быть
потребителями API бюджета, но не частью данного контракта.

## Baseline текущего MVP

Budget enforcement отсутствует: текущий scheduler не проверяет лимиты и не
создаёт reservations. `BUDGETING.md` ранее описывал предварительный
ticket-level `budget_points`, но runtime его не читает.

Traceability остаётся за существующими источниками: подтверждённые
`codex_cli.turn.completed` события суммируются в `token_usage`, а malformed,
legacy или incomplete output получает `source=unknown`; значение сохраняется в
`.vibe/runs/<run_id>/run.json` и terminal `run_history`. `active_run`,
`run_history` и каталог `.vibe/runs/<run_id>` используют общий `run_id`.

`DeliverySession` хранит `ticket_ids`, lifecycle и `audit_events` и ограничивает
выбор Delivery-тикетов. Сейчас `needs_rework` создаёт child типа `rework` с
`parent` и `blocked_by`; `wip_exempt` относится только к WIP и пока не имеет
бюджетной связи.

Новый ledger не подменяет эти источники и не реконструирует старое потребление.
История запусков не является budget ledger.

## Термины, scopes и ownership

Контракт вводит три scope:

- `session` владеет лимитом всей активной Delivery-сессии и агрегирует
  effective cost всех входящих ticket/run, включая rework;
- `ticket` владеет лимитом исходного Delivery-тикета; лимит распространяется
  на initial, retry и все его rework;
- `run` владеет одной immutable reservation/finalization записью и никогда не
  создаёт самостоятельный общий лимит.

Для каждого Delivery run обязателен `ticket_budget_id`. Если исходный ticket
входит в активную сессию, устанавливается `session_budget_id`. Retry получает
`parent_run_id`, rework — `parent_ticket_id`. Один run учитывается в каждом
применимом агрегате ровно один раз.

Rework расходуется из бюджета исходного ticket и, при активной сессии, из
бюджета этой session; child budget не создаётся. Это правило действует даже при
`wip_exempt=true`.

## Схема `budget.v1`

Budget record содержит обязательные поля:

```yaml
contract_version: budget.v1
budget_id: <stable-id>
scope: session # session | ticket
owner_id: <session.id-or-ticket.id>
mode: enforced # enforced | legacy
status: active
limits:
  limit_tokens: 100000 # non-negative integer or null
  limit_points: 100 # non-negative integer or null
  limit_runs: 10 # non-negative integer or null
aggregates:
  planned: {tokens: 0, points: 0, runs: 0}
  reserved: {tokens: 0, points: 0, runs: 0}
  finalized: {tokens: 0, points: 0, runs: 0}
  available: {tokens: 100000, points: 100, runs: 10}
created_at: <timestamp>
updated_at: <timestamp>
```

`limit_tokens`, `limit_points` и `limit_runs` — независимые неотрицательные
целые либо `null`; `null` означает отсутствие enforcement по этому измерению,
а не нулевой лимит. `planned` — сумма planned активных reservations и будущих
обязательств. `reserved` — удержанное значение незавершённых runs; reservation
считает один run. `finalized` — подтверждённый actual terminal runs; finalized
run также считается один раз. Для каждого измерения:

`available = limit - finalized - reserved`.

При отсутствии лимита `available` равен `null`. В enforced mode reservation
допустима только если `finalized + reserved + planned <= limit` по каждому
измерению с заданным лимитом. Проверки session и ticket выполняются атомарно
под общим lock/transaction.

## Run ledger

Каждый run получает одну запись до запуска:

```yaml
contract_version: budget.v1
run_id: <uuid>
ticket_id: <id>
ticket_budget_id: <id>
scope_links:
  session_budget_id: <id-or-null>
  parent_run_id: <id-or-null>
  parent_ticket_id: <id-or-null>
attempt_kind: initial # initial | retry | rework
planned: {tokens: 10000, points: 10, runs: 1}
reserved: {tokens: 10000, points: 10, runs: 1}
state: reserved # reserved | finalized | released | unknown
reserved_at: <timestamp>
```

`planned` фиксируется до запуска политикой stage/model/reasoning effort и имеет
`normalization_version`; задним числом он не меняется. `reserved` равно
принятому к удержанию planned и освобождается только terminal transition.

Для `finalized` обязательно `actual` с raw tokens, normalized points,
`usage_source`, `usage_ref`, `normalization_version` и `rate_card_version`.
`run_id` — уникальный ключ reservation/finalization: повторный polling или
обработка результата не меняет агрегаты повторно.

После `finalized`, `released` или `unknown` запись immutable. Исправление не
редактирует её, а добавляет отдельный immutable adjustment record с
`adjusts_run_id`, `reason`, `author` и `timestamp`.

## Состояния и precedence

- `active` — новые runs разрешены при доступном лимите и отсутствии unknown;
- `stop_new_runs` — ручной или policy gate запрещает новые runs, но существующие
  reservations могут завершиться;
- `exhausted` — available равен нулю хотя бы по одному enforced измерению,
  поэтому положительный новый planned запрещён;
- `over_budget` — finalized превышает лимит хотя бы по одному измерению;
- `blocked_unknown` — есть run `state=unknown`; usage не считается нулём;
- `completed` — scope явно закрыт, новые reservations запрещены, история и
  adjustments доступны для чтения.

При вычислении статуса действует детерминированный порядок:

`over_budget` → `blocked_unknown` → `stop_new_runs` → `completed` → `exhausted` → `active`.

## Lifecycle, usage и overrun

Перед вызовом Codex control plane атомарно создаёт reservation. При отказе по
лимиту Codex не запускается, `failed` run не создаётся и зависший reservation
не остаётся. Ошибка до создания подпроцесса освобождает reservation без actual.
Ошибка после старта финализируется по тем же правилам, что и обычный terminal
result.

Подтверждённый provider usage, коррелированный с `run_id`, становится actual.
Fallback допустим только с явными `source=runner_fallback`,
`normalization_version` и `rate_card_version`. При отсутствии подтверждённого
или разрешённого fallback reservation снимается, finalized не увеличивается,
run получает `unknown`, а scope — `blocked_unknown`; следующий run запрещён до
ручного решения.

Если actual больше planned, сохраняется весь actual, Codex не прерывается, а
после финализации scope становится `over_budget` при превышении лимита.

Raw token counts хранятся отдельно от normalized budget points. Каждая planned
и actual point value обязана иметь `normalization_version`; rate card фиксирует
версию таблицы стоимости/пересчёта и не пересчитывает прошлые записи. Отсутствие
конверсии в points не превращается в подтверждённый ноль.

## Retry, rework и membership

Retry получает новый `run_id`, отдельную reservation и тот же ticket budget;
предыдущая reservation не переиспользуется. Rework получает child ticket и
`attempt_kind: rework`, но его cost входит в budget исходного ticket и active
session ровно один раз. `wip_exempt` не обходит budget gate. Один ticket может
принадлежать не более чем одной active Delivery session; membership после
активации сессии не изменяется.

## Legacy и миграция

Если budget record отсутствует или `mode=legacy`, scheduler не блокирует запуск
по бюджету и автоматически не создаёт reservation. Существующие ticket/session
и `run_history` остаются читаемыми. Старый `token_usage` не переносится в
finalized без доказуемого `usage_ref` и версий нормализации.

Переход `legacy` → `enforced` выполняется явной миграцией конкретного scope с
заданными лимитами. Прошлые runs остаются historical/excluded, если их нельзя
надёжно нормализовать.

Миграция ticket добавляет только budget metadata и стабильно связывает budget с
исходным Delivery ticket; `parent`, `status`, `blocked_by`, `active_run` и
`run_history` не меняются. Session budget связывается с `session.id`; сохраняются
`ticket_ids`, lifecycle timestamps и `audit_events`. Старые
`.vibe/tickets/**/run_history` не переписываются и synthetic run records без
проверяемого источника не создаются. Для rework вычисляется `parent_ticket_id`,
отдельный лимит не появляется. Откат metadata не меняет lifecycle и
`run_history`; ledger records остаются доступными для аудита.

Предлагаемое хранилище: `.vibe/budgets/<budget_id>.yaml`, append-only
`.vibe/budgets/ledger.jsonl` и `.vibe/budgets.lock`. Конкретный authoritative
layout должен быть подтверждён до enforcement; append/update reservation и
finalization обязаны быть atomic, с lock и recovery для сбоя между reservation
и стартом процесса.

## Acceptance scenarios

1. При available 30 и planned 20 создаётся ровно одна reservation; повторный
   run с тем же `run_id` идемпотентен.
2. actual 17 переводит reservation в finalized, уменьшает reserved и увеличивает
   finalized на 17; available пересчитывается по формуле.
3. `limit_tokens`, `limit_points` и `limit_runs` проверяются независимо;
   превышение любого enforced измерения запрещает новый run.
4. Retry и rework используют существующие ticket/session budgets и не дают
   двойного списания; child rework не получает отдельный лимит.
5. Unknown usage даёт `blocked_unknown`, а completed scope не принимает новые
   reservations.
6. Actual выше planned сохраняется полностью, с версиями нормализации и rate
   card, и даёт `over_budget` без остановки уже запущенного процесса.
7. Изменение policy/rate card не меняет прошлые planned/actual; исправление —
   отдельный immutable adjustment.
8. Legacy migration сохраняет lifecycle и `run_history` без их переписывания.

## Зависимости и открытые решения

До enforcement нужно подтвердить гарантию корреляции provider usage с каждым
`run_id`, владельца normalization table и формат `rate_card_version`, а также
authoritative persistence layout. Manual override для `over_budget` и
`blocked_unknown` требует audit actor/reason и отдельного решения о полномочиях.
Срок хранения ledger и UI/CLI ролей также остаются вне этого контракта.
