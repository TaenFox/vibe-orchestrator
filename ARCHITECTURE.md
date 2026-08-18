# Архитектура

## Разделение ответственности

**Репозиторий оркестратора**
- определения workflow
- планирование WIP/pull
- провайдер промптов (файлы в прототипе)
- запуск воркеров Codex
- минимальный UI

**Репозиторий целевого проекта**
- `.vibe/tickets/**`
- исходный код
- знания/документация проекта
- артефакты реализации

**Будущий KMS**
- версионируемые промпты и политики, доступные по API
- замена файлового провайдера промптов без изменений scheduler/tickets

**Budget control plane (`budget.v1`)**
- scopes `session`, `ticket` и `run` с ownership и связями через `run_id`;
- независимые `limit_tokens`, `limit_points` и `limit_runs`, агрегаты
  `planned`/`reserved`/`finalized`/`available`;
- atomic ledger reservations/finalizations для initial, retry и rework без
  отдельного бюджета child rework;
- состояния `active`, `stop_new_runs`, `exhausted`, `over_budget`,
  `blocked_unknown` и `completed` с фиксированным precedence;
- версионирование normalization/rate card и immutable adjustments.

Контракт принят, но enforcement в текущем MVP не реализован: scheduler не
проверяет лимит и не резервирует ресурс. Бюджетный ledger не является частью
текущей модели `Ticket` и не подменяет `run_history` или каталог
`.vibe/runs/<run_id>`; при реализации он должен ссылаться на них через тот же
`run_id`. Legacy migration не переписывает lifecycle-поля или `run_history`.
Границы и полная схема описаны в [`BUDGETING.md`](BUDGETING.md).

## Терминология и membership

Тикет — единица control plane: у него ровно один текущий статус workflow и
ссылки на локальные артефакты. Поля `parent` и `blocked_by` имеют разные
семантики:

- `parent` задает membership — принадлежность тикета к агрегату родителя. Для
  Discovery → Delivery это связь Discovery-идеи с Delivery `story`/`task`/`bug`.
  Для Rework/Correction это связь корректирующей работы с тикетом, породившим
  ее.
- `blocked_by` — планировочный gate. Только ID в этом списке запрещают выбор
  тикета scheduler-ом. Наличие `parent` само по себе не блокирует родителя и не
  делает обычного ребенка WIP-exempt.

В MVP нет отдельной таблицы membership и нет materialized aggregate status:
члены агрегата находятся запросом `parent == <id>`. При повторном результате
`technical_analysis` отсутствующие в новом списке Delivery-дети
деактивируются (их `parent` очищается, статус становится `done`); похожие
тикеты по-прежнему не дедуплицируются.

## Жизненный цикл агента

1. Человек перемещает тикет из backlog в допустимую очередь (например, Discovery `ready`).
2. Планировщик сканирует все тикеты и проверяет WIP целевой стадии.
3. Оркестратор генерирует единый `run_id`, переводит тикет в активную агентную стадию и записывает его в `active_run`.
4. В `ticket.run_history` добавляется durable-событие `started` с `run_id`, stage, execution profile и идентичностью prompt.
5. Он запускает независимый подпроцесс `codex exec` в целевом репозитории и создает `.vibe/runs/<run_id>/run.json`.
6. Codex возвращает структурированный `{outcome, summary, details}`, а stdout раннера сохраняется в `.vibe/runs/<run_id>/events.jsonl`.
7. Оркестратор валидирует outcome по workflow YAML, пишет `completed`/`failed` в `run_history`, сохраняет `result.json` и применяет настроенный переход.
8. Следующая очередь может быть выбрана, когда ее WIP это позволяет.

Основной цикл оркестратора никогда не ждет агента синхронно: каждый вызов Codex работает как отдельная задача asyncio subprocess.

### Scheduler gate

Кандидат допускается к запуску только если у него нет `active_run` и
`blocked_by`, а текущий статус задает допустимое действие: queue с `pull_to`
ведет в указанную agent-стадию, agent с разрешенным outcome повторяет себя,
agent после сбоя повторяется только в пределах retry/backoff. При переходе из
queue обычный тикет не допускается, если WIP целевой agent-стадии исчерпан;
`wip_exempt` не учитывается в этом лимите. Выбор глобально упорядочен по правой
позиции workflow, затем по exempt-классу, `priority`, возрасту и ID. Это gate
планировщика, а не бизнес-правило закрытия и не бюджетный контроль.

## Контракт traceability

Аудит опирается на два источника, и у них разная роль:

- `ticket.active_run` — only live pointer. Показывает, какой запуск сейчас выполняется, и очищается после завершения или ошибки.
- `ticket.run_history[]` — durable ticket history. Это журнал ссылок на каждый запуск, который должен переживать очистку `active_run` и использоваться в ретроспективе.
- `.vibe/runs/<run_id>/` — run artifacts. Каталог содержит локальные файлы конкретного запуска и связывается с тикетом через `run_history[].artifacts_path`.

Инварианты MVP:

- Один запуск тикета на одной агентной стадии имеет ровно один `run_id`.
- Тот же `run_id` обязан совпадать в `active_run`, `run_history[].run_id`, prompt и имени каталога `.vibe/runs/<run_id>`.
- `run.json` является source of truth для execution profile конкретного запуска (`model`, `reasoning_effort`), prompt-контракта (`prompt_path`, `prompt_version`, `prompt_contract`) и базовой идентичности (`ticket_id`, `process`, `stage`).
- `prompt_version` должен меняться при любом дрейфе реально исполняемого prompt-контракта: как при изменении markdown prompt, так и при изменении wrapper/instructions, которые Codex получает поверх него.
- `prompt_version` должен воспроизводимо пересчитываться из сохраненных артефактов запуска: канонический prompt-контракт сериализуется в `.vibe/runs/<run_id>/prompt.contract.txt` и `run.json["prompt_contract"]`, после чего аудитор может проверить `sha256` без доступа к исходному workflow-коду.
- `result.json` является source of truth для структурированного ответа агента.
- `events.jsonl` нужен как низкоуровневый сырой след исполнения и не заменяет `run_history`.

Рекомендованный порядок расследования:

1. Найти нужный `run_id` в `ticket.run_history`.
2. Открыть `.vibe/runs/<run_id>/run.json` и проверить stage plus execution profile.
3. Сопоставить `result.json` с переходом workflow и `last_outcome`/`last_summary`.
4. Использовать `events.jsonl`, только если нужен сырой вывод CLI.

## Корректирующая работа

`rework` и `correction` — это тикеты с `wip_exempt: true`. У них есть `parent`, а сам оркестратор создает их автоматически на outcomes `needs_rework` и `needs_correction`.

- Родитель остается на текущей агентной стадии и продолжает занимать ее WIP.
- Дочерний тикет автоматически стартует из первой очереди процесса (`selected_for_session` для Delivery, `ready` для Discovery).
- Пока дочерний тикет не завершен, его идентификатор находится в `parent.blocked_by`, поэтому родитель не может быть повторно выбран планировщиком.
- После завершения всех связанных Rework/Correction оркестратор автоматически снимает блокировку, и родитель снова становится доступен для повторного прохождения той же стадии.

## Связь Discovery → Delivery

Discovery `technical_analysis` может вернуть в `details` YAML-блок `delivery_tickets`. Оркестратор парсит его и автоматически создает связанные Delivery-тикеты типов `story`, `task` и `bug`, привязывая их через `parent` и сохраняя флаг `mandatory`.

Стадия Discovery `implementation` работает как `wait`-барьер:

- пока существует хотя бы один незавершенный обязательный (`mandatory: true`) связанный Delivery-тикет, Discovery-идея остается на `implementation`;
- когда все обязательные связанные Delivery-тикеты завершены, оркестратор автоматически переводит идею в `ready_for_validation`.

### Legacy-режим и миграция

До появления явного решения технического анализа Discovery-тикет может не
иметь `implementation_required`. Такой тикет не требует массовой миграции:
загрузка YAML подставляет значения по умолчанию для отсутствующих полей, а
`next_status_for_ticket()` применяет совместимый fallback:

- есть хотя бы один Delivery-ребенок по `parent` → `implementation`;
- детей нет → `ready_for_validation`.

Миграция выполняется постепенно: старые YAML можно оставить без изменений,
а новые результаты `technical_analysis` должны записывать boolean явно. Если
нужно нормализовать старый YAML вручную, сначала проверьте его Delivery-членов
по `parent`, затем добавьте `implementation_required: true` при наличии
обязательных детей или `false` при отсутствии реализации; существующие
`parent`, статусы и историю запусков при этом не переписывайте. Поле не следует
добавлять автоматически без проверки состава агрегата.

Для нового потока `technical_analysis` обязан вернуть boolean
`implementation_required`. При `true` нужен минимум один Delivery-тикет с
`mandatory: true`; при `false` список `delivery_tickets` должен быть пустым.
После выбора `implementation` переход к валидации — это wait-gate: при
`implementation_required != false` незавершенный mandatory-ребенок удерживает
идею, а optional-ребенок не удерживает. `done` для Delivery означает, что
ветка тикета успешно интегрирована в целевую ветку.

### Parent и агрегаты закрытия

Parent не закрывается автоматически завершением обычного Delivery-ребенка:
Discovery закрывается только своей валидацией, а Delivery — своим release.
Для Delivery release интегрирует ветку в ветку Delivery-родителя, если parent
является Delivery; иначе — в `main`. После успешной интеграции ребенок получает
`done`.

Rework/Correction — исключение для runtime-блокировки, а не общий aggregate
close: их ID добавляются в `parent.blocked_by`, пока корректирующая работа не
разрешена. Delivery Rework считается разрешенным уже с
`ready_for_release` (и потому может разблокировать родителя до фактического
release), Discovery Correction — только в `done`. После разрешения всех
записей `blocked_by` родитель снова может повторить свою стадию.

## Ограничения MVP

- `.vibe/runs/` намеренно остается локальным и игнорируется Git, поэтому для долгого хранения аудит опирается на `run_history` в YAML тикета.
- Протокол traceability не защищает от ручного редактирования файлов `.vibe/tickets/**`; доверие к аудиту опирается на дисциплину репозитория и Git history.
- Enforcement budget.v1, cost model, capacity planning и агрегированные
  финансовые/трудовые показатели не реализованы; `priority`, `wip`, `mandatory`
  и статусы не следует интерпретировать как бюджетные значения. Модель и
  migration contract зафиксированы в [`BUDGETING.md`](BUDGETING.md).
