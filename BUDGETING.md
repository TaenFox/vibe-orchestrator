# Версионируемый контракт budget control plane

Статус: принятый контракт `budget.v1`; runtime ledger реализован для Delivery.
Этот документ фиксирует модель, baseline и границы реализации.

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

В legacy-режиме scheduler сохраняет прежнее поведение. Для enforced budget
records scheduler атомарно резервирует лимиты до запуска, а SQLite ledger
становится authoritative источником reservations и агрегатов. Ранее описанный
ticket-level `budget_points` runtime не читал.

Связка выполняется в `_schedule_once`: фактический `ticket_id` запуска берётся
из выбранного Delivery-тикета, а ticket scope передаётся в ledger отдельно.
Для initial/retry это тот же ticket, для rework — исходный parent. Budget gate
срабатывает до запуска Codex.

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

## Authoritative SQLite layout

`BudgetLedger` открывает `.vibe/budgets/ledger.sqlite3`, включает foreign keys,
WAL, busy timeout и использует `BEGIN IMMEDIATE` для операций записи. Таблицы
`budgets`, `runs`, `adjustments` и `metadata` хранят scope aggregates, immutable
run state и append-only corrections. Отсутствующий budget record или
`mode=legacy` означает bypass без synthetic ledger run.

## Термины, scopes и ownership

Контракт вводит три scope:

- `session` владеет лимитом всей активной Delivery-сессии и агрегирует
  effective cost всех входящих ticket/run, включая rework;
- `ticket` владеет лимитом исходного Delivery-тикета; лимит распространяется
  на initial, retry и все его rework;
- `run` владеет одной immutable reservation/finalization записью и никогда не
  создаёт самостоятельный общий лимит.

Для каждого Delivery run `ticket_id` — фактический ticket, создавший запуск, а
`ticket_budget_id` — budget owner scope. Для initial/retry оба указывают на
один ticket; для rework `ticket_id` остаётся child, а `ticket_budget_id`
ссылается на budget исходного parent. Если child входит в активную сессию,
устанавливается `session_budget_id` текущей session. Retry получает
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
  planned: {tokens: 0, points: 0, runs: 0} # незарезервированные obligations
  reserved: {tokens: 0, points: 0, runs: 0}
  finalized: {tokens: 0, points: 0, runs: 0}
  available: {tokens: 100000, points: 100, runs: 10}
created_at: <timestamp>
updated_at: <timestamp>
```

`limit_tokens`, `limit_points` и `limit_runs` — независимые неотрицательные
целые либо `null`; `null` означает отсутствие enforcement по этому измерению,
а не нулевой лимит. `planned` — только сумма зафиксированных будущих
обязательств, для которых ещё нет active reservation. `reserved` — удержанное
значение active reservations незавершённых runs. `finalized` — подтверждённый
actual terminal runs; каждый run учитывается один раз.

Для каждого измерения едины определения:

`committed = finalized + planned + reserved`

`available = limit - committed`.

При отсутствии лимита `available` равен `null`, и измерение не участвует в gate.
Для нового обязательства reservation допустима, если
`finalized + planned + reserved + requested_planned <= limit` по каждому
enforced измерению. `requested_planned` — только новая стоимость, ещё не
включённая в `planned`; после успешной операции она сразу попадает в
`reserved`, а не остаётся одновременно в `planned`.

Если reservation принимает уже существующее planned obligation, операция
атомарно уменьшает `planned` на held value и увеличивает `reserved` на ту же
величину. Для такого transfer gate проверяет текущий `committed` (новая
стоимость не прибавляется второй раз), поэтому `available` не меняется. При
прямом запуске obligation создаётся и сразу переводится в `reserved`, без
промежуточного увеличения `planned`. Проверки session и ticket выполняются
атомарно под общим lock/transaction.

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
planned: {tokens: 10000, points: 10, runs: 1, normalization_version: v1, points_status: available}
reserved: {tokens: 10000, points: 10, runs: 1}
state: reserved # reserved | finalized | released | unknown
reserved_at: <timestamp>
```

`planned` в записи run — immutable estimate, зафиксированный до запуска
политикой stage/model/reasoning effort. `planned.normalization_version` обязана
быть непустой при `planned.points != null`; при `planned.points: null` она
обязана быть `null`, а `planned.points_status` обязан быть `unavailable`.
`null` не означает нулевое потребление и не может тихо пройти gate для
`limit_points`. `reserved` — immutable held value, принятый из planned при
reservation; он освобождается только terminal transition.

Для `finalized` обязательно `actual` с raw tokens, normalized points,
`usage_source`, `usage_ref`, `normalization_version`, `rate_card_version` и
`points_status`:

```yaml
actual:
  tokens_raw: 17000
  points: 17
  points_status: available # available | unavailable
  usage_source: provider
  usage_ref: <provider-event-id>
  normalization_version: v1 # required iff points != null; otherwise null
  rate_card_version: v1
```

При `actual.points: null` `actual.normalization_version` обязана быть `null`,
а `points_status` — `unavailable`. Если для scope задан enforced
`limit_points` (включая `0`), такая finalization не подтверждает usage: run
получает `unknown`, scope — `blocked_unknown`, и actual не увеличивает
`finalized` как ноль. Если `limit_points: null`, unknown остаётся
audit-сигналом, но не блокирует scope; остальные измерения могут быть
финализированы.
`run_id` — уникальный ключ reservation/finalization: повторный polling или
обработка результата не меняет агрегаты повторно.

После `finalized`, `released` или `unknown` запись immutable. Исправление не
редактирует её, а добавляет отдельный immutable adjustment record с
`adjusts_run_id`, `reason`, `author` и `timestamp`.

## Состояния и precedence

- `active` — новые runs разрешены при доступном лимите и отсутствии применимого
  unknown (только для enforced scope с `limit_points IS NOT NULL`);
- `stop_new_runs` — ручной или policy gate запрещает новые runs, но существующие
  reservations могут завершиться;
- `exhausted` — available равен нулю хотя бы по одному enforced измерению,
  поэтому положительный новый planned запрещён;
- `over_budget` — finalized превышает лимит хотя бы по одному измерению;
- `blocked_unknown` — есть run `state=unknown` в scope с enforced
  `limit_points IS NOT NULL`; usage не считается нулём;
- `completed` — scope явно закрыт, новые reservations запрещены, история и
  adjustments доступны для чтения.

При вычислении статуса действует детерминированный порядок:

`over_budget` → `blocked_unknown` → `stop_new_runs` → `completed` → `exhausted` → `active`.

## Lifecycle, usage и overrun

### Baseline текущего MVP и подтверждённый usage fact

Delivery run проходит состояния `reserved_pending_start -> started -> finalized|unknown|released`. Provider usage является единственным источником фактических токенов; prompt, summary, details, result payload и длина текста не являются usage-доказательством.

Normalized usage fact содержит `run_id`, raw `input_tokens`, `output_tokens`, `total_tokens`, `model`, `reasoning_effort`, `source`, `usage_ref`, `captured_at` и `normalization_version`. Для confirmed provider и runner fallback все эти provenance-поля и `captured_at` обязательны и непусты; provider event принимается только при exact correlation с run/profile, непустом stable ref и `total_tokens == input_tokens + output_tokens`.

Для contract-aware provider adapter `usage_ref` обязан быть stable reference уровня
event/snapshot: принимается явный `usage_ref`, а при его отсутствии —
`provider_event_id`. `provider_request_id` не является usage reference, потому что
может повторяться между несколькими `turn.completed`; он сохраняется только как
optional provenance metadata. Событие только с request ID не подтверждает usage и
исключается из расчёта (fail closed в `unknown`, если других валидных событий нет).

Incremental facts складываются по уникальному stable `usage_ref`; replay того же ref
с теми же counts идемпотентен, а тот же ref с изменившимися counts даёт `unknown`.
Поэтому два `turn.completed` с одним `provider_request_id`, но разными stable event
refs, суммируются как два distinct факта. Cumulative snapshots не складываются:
используется последний валидный snapshot, а regression, mixed semantics или
изменение counts у одного ref дают `unknown`. `runner_fallback` разрешён только с
`fallback_policy_version`, `normalization_version` и `degraded_confidence: true`.
Cost/currency и `rate_card_version` — optional audit metadata.

### Изменения тикета DEL-B09FBE

Adapter передаёт correlated contract без пересчёта в `run.json`, `result.json`, `run_history` и ledger. Ledger валидирует provenance на finalize boundary, сохраняет raw counts и metadata, а повторный finalize terminal run идемпотентен. Старые артефакты читаются через legacy parser, но `source=codex_cli.turn.completed` является только read-compatible форматом и никогда не считается confirmed или переносится в finalized aggregates. Если provider не поставляет correlation/stable event-or-snapshot ref/version/timestamp, результат остаётся `unknown`; upstream должен явно определить точное поле stable ref и гарантировать его стабильность на уровне события или snapshot. `provider_request_id` остаётся только optional metadata и не может заменять эту гарантию.

Перед вызовом Codex control plane атомарно создаёт reservation. При отказе по
лимиту Codex не запускается, `failed` run не создаётся и зависший reservation
не остаётся. Ошибка до создания подпроцесса освобождает reservation без actual.
Ошибка после старта финализируется по тем же правилам, что и обычный terminal
result. В ledger `start` означает успешное возвращение
`asyncio.create_subprocess_exec`, а не создание asyncio task или запись
manifest; runner вызывает lifecycle callback непосредственно на этой границе.

При старте оркестратора и перед каждой попыткой schedule выполняется
`reconcile()` для просроченных `reserved_pending_start`. Явно отсутствующий
процесс освобождает reservation, подтверждённо присутствующий переводит run в
`started`, а неразрешимый случай оставляет run и получает marker
`ambiguous_start`. Без evidence resolver запись не освобождается по одному
таймауту: безопасный fallback сохраняет ambiguous.

Подтверждённый provider usage, коррелированный с `run_id`, становится actual.
Fallback допустим только с явными `source=runner_fallback`,
`normalization_version` и `rate_card_version`. При отсутствии подтверждённого
или разрешённого fallback reservation снимается, finalized не увеличивается,
run получает `unknown`. Scope с enforced `limit_points IS NOT NULL` получает
`blocked_unknown` и запрещает следующий run до ручного решения; scope с
`limit_points: null` сохраняет unknown для аудита и может продолжать работу по
остальным лимитам.

Если actual больше planned, сохраняется весь actual, Codex не прерывается, а
после финализации scope становится `over_budget` при превышении лимита.

Raw token counts хранятся отдельно от normalized budget points. Каждая ненулевая
planned/actual point value обязана иметь `normalization_version`; при null
conversion версия null и status `unavailable`. Rate card фиксирует версию
таблицы стоимости/пересчёта и не пересчитывает прошлые записи. Отсутствие
конверсии в points не превращается в подтверждённый ноль. Каждый связанный
enforced scope с `limit_points IS NOT NULL` получает `blocked_unknown`; scope с
`limit_points: null` не блокируется этим unknown. Этот reserve gate автоматически
не сбрасывается.

Terminal run immutable. Исправление выполняется только append-only
`adjustment` с signed delta, reason, author и timestamp. Positive и negative
delta применяются одной SQLite-транзакцией; resulting finalized aggregate не
может стать отрицательным, иначе adjustment и частичное изменение не создаются.

## Retry, rework и membership

### Delivery session scopes

DeliverySession хранит `budget_policy` (`legacy` или `enforced`),
`budget_limits` для `tokens`, `points` и `runs` (неотрицательное целое либо
`null`) и `membership_policy` (`legacy` или `required`). Новая сессия с
заданными лимитами автоматически получает `enforced`; при активации для неё
создаётся scope `session:<session-id>`. В legacy session отсутствие budget
record сохраняет прежний bypass.

При `membership_policy: required` запуск любого Delivery ticket, включая
`wip_exempt` rework, возможен только после явного включения в session. Для
исключения используется отдельный override с непустыми `actor` и `reason`;
он записывается в `audit_events` вместе с `ticket_id` и timestamp. Флаг
`wip_exempt` сам по себе membership не заменяет.

Для enforced session `reserve` в одной `BEGIN IMMEDIATE` транзакции проверяет
и ticket scope, и `session:<session-id>`; отсутствие или нехватка любого scope
отклоняет reservation без частичных run/aggregate. Initial/retry используют
текущий ticket, rework — `parent_ticket_id`; child budget не изменяется.

Complete переводит session scope в `completed`, cancel — в `stop_new_runs`.
Оба перехода сохраняют YAML, audit events, scopes, runs и aggregates, поэтому
уже начатый run может быть finalized/reconciled, а новые reservations через
terminal session запрещены. Старые session YAML без новых полей читаются с
безопасными legacy defaults и при сохранении получают полную схему; история
тикетов и synthetic runs при миграции не создаются.

Retry получает новый `run_id`, отдельную reservation и тот же ticket budget;
предыдущая reservation не переиспользуется. Rework получает child ticket и
`attempt_kind: rework`, но его cost входит в budget исходного ticket и active
session ровно один раз. Child ID сохраняется в run traceability, parent ID — в
`parent_ticket_id` и выборе ticket budget. Child budget не создаётся и не
выбирается, даже если такая запись существует. Отсутствующий parent блокирует
rework до запуска. `wip_exempt` не обходит budget gate. Один ticket может
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

Реализованное authoritative-хранилище — SQLite
`.vibe/budgets/ledger.sqlite3`; append/update reservation и finalization
выполняются atomic под `BEGIN IMMEDIATE`. Состояния проходят
`reserved_pending_start -> started -> finalized|released|unknown`.
`reconcile()` освобождает только явно подтверждённый отсутствующий запуск,
переводит подтверждённый запуск в `started`, а неоднозначный оставляет с
marker `ambiguous_start`.

Lifecycle invariant: `finalize` разрешён только для `started`; попытка завершить
`reserved_pending_start` отклоняется без изменения reservation или aggregates.
После terminal transition status scope вычисляется из текущих aggregates и
unknown runs с precedence `over_budget` → `blocked_unknown` → `stop_new_runs` →
`completed` → `exhausted` → `active`. Для finalized run actual сохраняется с
`runs: 1`, если usage не указал это измерение, а повторная terminal обработка
остаётся идемпотентной.

## Acceptance scenarios

1. Если до нового запуска `available=30`, а `requested_planned=20`, gate
   принимает reservation: `reserved` увеличивается на 20, новая стоимость не
   попадает в `planned`, а post-reservation `available=10`. Повторная операция
   с тем же `run_id` возвращает существующую запись и не меняет aggregates.
2. Если существующее planned obligation равно 20, его transfer в reservation
   уменьшает `planned` на 20 и увеличивает `reserved` на 20; `committed` и
   `available` остаются неизменными. Затем actual 17 уменьшает reserved на held
   value, увеличивает finalized на 17 и пересчитывает available.
3. `limit_tokens`, `limit_points` и `limit_runs` проверяются независимо;
   превышение любого enforced измерения запрещает новый run.
4. Retry использует собственный ticket budget, а rework резервирует parent
   ticket budget и budget active session; child budget не изменён. Run хранит
   child ID и `parent_ticket_id`, а после finalize/release оба агрегата меняются
   ровно один раз. Отказ parent/session блокирует запуск без reservation.
5. Unknown usage даёт `blocked_unknown` только scope с enforced
   `limit_points IS NOT NULL`, а completed scope не принимает новые
   reservations.
6. Ненулевые planned/actual points требуют normalization version; при null
   points version null и status unavailable, а активный point limit приводит к
   unknown, не к финализации нулём.
7. Actual выше planned сохраняется полностью, с версиями нормализации и rate
   card, и даёт `over_budget` без остановки уже запущенного процесса.
8. Изменение policy/rate card не меняет прошлые planned/actual; исправление —
   отдельный immutable adjustment.
9. Legacy migration сохраняет lifecycle и `run_history` без их переписывания.
10. AC-4: pre-start finalize отклоняется; после `start` тот же run финализируется.
11. AC-7: finalized overrun даёт `over_budget`, нулевой available даёт
    `exhausted`, а unknown блокирует scope только при enforced
    `limit_points IS NOT NULL` (включая `0`). При `limit_points: null`
    подтверждённые остальные измерения финализируются, а следующий run
    разрешается при доступных прочих лимитах.
12. AC-8: каждый finalized run увеличивает `finalized.runs` ровно на один;
    повторный finalize не меняет агрегат.
13. Contract-aware incremental adapter суммирует два события с одним
    `provider_request_id`, если у них разные `provider_event_id`/`usage_ref`;
    повтор того же stable ref с теми же counts учитывается один раз.
14. Повтор stable ref с изменившимися counts даёт `unknown`, а событие только
    с `provider_request_id` не становится confirmed usage.

## Зависимости и открытые решения

Нужно подтвердить гарантию корреляции provider usage с каждым `run_id`, точное
upstream-поле stable event/snapshot ref и гарантию его уникальности/стабильности;
`provider_request_id` может повторяться и остаётся только metadata. Также нужно
подтвердить владельца normalization table и формат `rate_card_version`. Manual
override для `over_budget` и `blocked_unknown` требует audit actor/reason и
отдельного решения о полномочиях.
Срок хранения ledger и UI/CLI ролей также остаются вне этого контракта.
