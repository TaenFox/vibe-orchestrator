# vibe-orchestrator

Прототип pull-оркестратора на основе тикетов для локальных Codex CLI-агентов.

Оркестратор отвечает за **правила процесса и промпты**. Репозиторий целевого проекта отвечает за **тикеты, код, знания и рабочие артефакты**. Каждый запуск агента — это новый вызов `codex exec` для одного тикета на одной стадии.

## Текущая модель

### Термины и границы контракта

- **Тикет** — YAML-агрегат control plane с одним текущим `status`; код, знания и
  артефакты реализации находятся в целевом репозитории и не становятся полями
  тикета.
- **Parent** (`parent`) — семантическая связь «эта работа относится к
  родителю». Она формирует membership: тикет входит в состав родителя, если его
  `parent` равен ID родителя. Это не означает, что родитель заблокирован.
- **Блокировка** (`blocked_by`) — отдельная runtime-связь, из-за которой тикет
  нельзя планировать. Автоматически блокируются только родительские тикеты при
  создании Rework/Correction; обычный Delivery-ребенок с `parent` не блокирует
  своего родителя.
- **Агрегат родителя** — сам родитель плюс его текущие связанные дети. Для
  Discovery → Delivery в агрегат входят Delivery-тикеты с `parent` и типом
  `story`, `task` или `bug`; `mandatory` определяет только обязательность для
  gate реализации.
- **Бюджет** в MVP отсутствует: нет денежных, человеко-часовых, token- или
  cost-полей, расчета, лимита и решения по бюджету. `priority` — порядок
  планирования, а `wip` — лимит одновременно выполняемых обычных тикетов, не
  бюджет.

В комплект входят три процесса:

- **Discovery** — Идея → анализ → Technical Analysis → Investment Decision → ожидание реализации → валидация, плюс дочерние Correction без учета в WIP.
- **Delivery** — Story / Task / Bug плюс дочерние Rework без учета в WIP.
- **Process Management** — Audit / Planning / Estimation.

Основные правила:

- Сначала выбирается **самая правая доступная очередь**, затем корректирующая работа, приоритет и возраст.
- Для активных стадий действуют лимиты WIP.
- Оркестратор забирает тикет, перемещая его в активную стадию **до** запуска Codex.
- Outcomes `needs_rework` и `needs_correction` автоматически создают дочерние тикеты Rework/Correction, не учитываемые в WIP; родитель остается на своей текущей стадии, заблокирован и продолжает занимать WIP. Rework разблокирует родителя после успешной приемки, а Correction проходит формирование и подтверждение человеком, после чего сразу закрывается и разблокирует родителя.
- Discovery `technical_analysis` явно указывает `implementation_required` и может автоматически создать связанные Delivery-тикеты `story` / `task` / `bug`. После инвестиционного решения идея без реализации сразу направляется в `ready_for_validation`; идея с реализацией ждет в `implementation` завершения обязательных (`mandatory: true`) Delivery-тикетов.
- Агенты возвращают `outcome`; сами статусы workflow они не меняют.
- При сбое агента оркестратор автоматически повторяет ту же стадию через 5 и 30 секунд. После третьего последовательного сбоя автоматические попытки прекращаются; новый цикл можно запустить кнопкой `Повторить` в UI.
- Delivery-тикеты проходят все агентные стадии в собственном Git worktree. `ready_for_release` автоматически интегрирует ветку тикета в ветку родителя или `main`; успешная интеграция закрывает тикет.
- Контракт будущего бюджетирования описан в [`BUDGETING.md`](BUDGETING.md); бюджетирование в MVP не реализовано.

Планировщик применяет scheduler gate в таком порядке: тикет не должен иметь
`active_run` или `blocked_by`; источник должен быть queue со связью `pull_to` на
agent-стадию либо разрешенным повтором той же agent-стадии; для перехода из
queue обычный тикет проходит WIP этой стадии. `wip_exempt: true` исключает тикет
из WIP и дает ему приоритет при глобальном выборе. Затем выбираются более
правые стадии, после чего учитываются тип работы, `priority`, возраст и ID.
Сам запуск создает `active_run` и переводит тикет в agent-стадию до вызова
Codex; агент не меняет статусы самостоятельно.

## Требования

- Python 3.11+
- Git
- Установленный и аутентифицированный Codex CLI (`codex --version`)
- Рекомендуется VS Code

На macOS `python3` или `/usr/bin/python3` могут по-прежнему указывать на более старый системный Python, не подходящий под требование `3.11+`. Сначала проверьте интерпретатор:

```bash
python3 --version
```

Если команда выводит версию ниже `3.11`, установите более новый Python и используйте этот исполняемый файл явно. Пример с Homebrew:

```bash
brew install python@3.11
python3.11 --version
```

Codex вызывается как `codex exec --sandbox workspace-write --json --model ... -c 'model_reasoning_effort="..."' --output-schema ... -o ... -`, используя уже настроенную аутентификацию CLI.

Execution profile задается явно на уровне каждого `kind: agent` stage в `workflows/*.yaml`; `load_workflow()` валидирует наличие `prompt`, `model` и `reasoning_effort`. Итоговые `model`, `reasoning_effort`, `prompt_path` и `prompt_version` попадают в prompt, `ticket.run_history` и `.vibe/runs/<run_id>/run.json`.

## Быстрый старт в VS Code

```bash
git clone https://github.com/TaenFox/vibe-orchestrator.git
cd vibe-orchestrator
PYTHON_BIN="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)"
$PYTHON_BIN -c 'import sys; raise SystemExit("Python 3.11+ is required" if sys.version_info < (3, 11) else 0)'
$PYTHON_BIN -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Откройте этот репозиторий в VS Code. Встроенные задачи покрывают настройку, тесты, оркестратор и команды UI.

Инициализируйте целевой Git-репозиторий:

```bash
vibe init /path/to/your-project
```

Добавьте идею:

```bash
vibe add /path/to/your-project discovery idea "Моя идея" \
  --description "Что я хочу исследовать"
```

Запустите UI:

```bash
vibe ui /path/to/your-project
```

Откройте `http://127.0.0.1:8765`. Переместите идею Discovery из **К выполнению** в **Готово**. `ready` — это очередь обязательств под контролем человека: агент никогда не забирает задачи напрямую из `todo`.

UI можно запустить вместе с оркестратором одной командой:

```bash
vibe run /path/to/your-project --ui
```

В верхней части UI доступна форма создания тикетов Discovery, Delivery и Process Management. Поля `тип`, `заголовок`, `описание`, `приоритет` и `родительский ID` сохраняются сразу в локальное состояние `.vibe/tickets`.

Во втором терминале VS Code:

```bash
vibe run /path/to/your-project
```

Оркестратор опрашивает тикеты, соблюдает WIP и параллельно запускает независимые подпроцессы Codex.

Лимит воркеров можно менять без остановки оркестратора через UI или CLI:

```bash
vibe workers /path/to/your-project 0   # перестать брать новые тикеты
vibe workers /path/to/your-project 2   # разрешить до двух запусков
vibe workers /path/to/your-project     # показать текущий лимит
vibe release-retry /path/to/your-project DEL-XXXXXX  # повторить merge после разрешения конфликта
```

Оркестратор перечитывает лимит перед каждым циклом планирования. Снижение лимита не прерывает уже запущенных агентов: новые запуски начнутся только после того, как число активных воркеров станет меньше заданного лимита.
Последний лимит сохраняется между перезапусками. Явный `--max-agents N` при старте переопределяет сохранённое значение.

При запуске из `stable` оркестратор автоматически находит checkout ветки `main` или создаёт временный worktree в `.vibe/tmp/worktrees/__main__`. `VIBE_MAIN_WORKTREE` и `VIBE_MAIN_BRANCH` можно использовать как override. Worktree тикета создаётся в `.vibe/tmp/worktrees/`; состояние тикетов остаётся только в `stable`. При конфликте release-операция делает `merge --abort`, оставляет тикет на `ready_for_release` и ждёт ручного разрешения перед `vibe release-retry`.

Для планирования целостной Delivery-сессии можно создать `.vibe/tmp/delivery-session.yaml`:

```yaml
active: true
participants:
  - DEL-XXXXXX
  - DEL-YYYYYY
```

Пока сессия активна, в `system_analysis` проходят только перечисленные тикеты;
`wip_exempt`-тикеты (например, rework) сохраняют прежнее поведение. При отсутствии
активного файла старые тикеты работают в legacy-режиме без миграции.

### Ограничения UI, telemetry и performance

UI проверяется регрессионными тестами для Discovery и Delivery: длинные заголовки
и описания экранируются и переносятся, `active_run` отражается на карточке и в
агрегате сессии, а API возвращает те же данные в JSON. Автообновление выполняется
раз в 5 секунд, но не запускается при открытых деталях или фокусе в поле ввода;
это ограничение предотвращает потерю незавершенного текста.

Интерфейс рассчитан на локальную работу и не собирает telemetry: браузер не
отправляет события, метрики или содержимое полей во внешний сервис. Рендеринг
доски и endpoint `/api/tickets` перечитывают локальные YAML-файлы целиком на
каждый запрос, поэтому UI не является пагинированным мониторингом для больших
объемов данных. Для MVP приемлемы локальные очереди и ручная проверка; при росте
числа тикетов потребуется отдельная оптимизация хранилища и обновления по diff.

Управляемые сессии можно создавать и изменять через CLI:

```bash
vibe session create /path/to/your-project "Релиз 1"
vibe session add /path/to/your-project SES-XXXXXX DEL-XXXXXX
vibe session activate /path/to/your-project SES-XXXXXX
vibe session show /path/to/your-project SES-XXXXXX
vibe session complete /path/to/your-project SES-XXXXXX
vibe session cancel /path/to/your-project SES-XXXXXX --override "Состав устарел"
```

Сессия активируется только с непустым составом Delivery-тикетов. Завершение или
отмена неполной сессии требуют `--override` (также поддерживается `--reason`) с
причиной. Состав черновика изменяется командами `session add` и `session remove`.

Для запуска из отдельного checkout `stable` можно указать целевой checkout ветки `main` через `VIBE_MAIN_WORKTREE`; если override не задан, оркестратор найдёт или создаст временный worktree автоматически. Перед `development` дерево тикета подтягивает актуальный локальный `main`. Worktree тикета создаётся в `.vibe/tmp/worktrees/`; состояние тикетов остаётся только в `stable`. При конфликте release-операция делает `merge --abort`, оставляет тикет на `ready_for_release` и ждёт ручного разрешения перед `vibe release-retry`.

## Хранение тикетов

Целевой репозиторий получает:

```text
.vibe/
├── README.md
├── .gitignore        # локальное состояние и артефакты игнорируются
├── runs/
│   └── <run_id>/
│       ├── prompt.contract.txt
│       ├── run.json
│       ├── events.jsonl
│       └── result.json
├── tmp/
│   └── workers.yaml  # локальный runtime-лимит воркеров
└── tickets/          # локальный радар stable, не часть code plane
    ├── discovery/
    ├── delivery/
    └── process_management/
```

Тикет — это один YAML-файл. Пример:

```yaml
id: DISC-A1B2C3
process: discovery
type: idea
title: Add family graph import
status: ready
priority: 100
parent: null
blocked_by: []
description: ...
wip_exempt: false
active_run: 8f2d6d9f10b1493c80d4a9dfcb0d9f2f
run_history:
  - run_id: 8f2d6d9f10b1493c80d4a9dfcb0d9f2f
    stage: technical_analysis
    event: started
    timestamp: 2026-08-16T09:00:00+00:00
    artifacts_path: .vibe/runs/8f2d6d9f10b1493c80d4a9dfcb0d9f2f
    prompt_path: discovery/technical_analysis.md
    prompt_version: sha256:...
    model: gpt-5.6-luna
    reasoning_effort: medium
    ticket_title: Add family graph import
    ticket_priority: "100"
    ticket_parent: none
    ticket_description: ...
```

Прототип намеренно **не** реализует базу данных, интеграцию с Jira, пользователей, права доступа и полный журнал событий.

## Traceability и аудит

Source of truth для аудита разделен на два слоя:

- `.vibe/tickets/**` — локальное долговечное состояние control plane. Оно принадлежит checkout `stable`, не входит в code plane и не переносится merge-операциями между ветками. Поля `status`, `active_run`, `last_outcome`, `last_summary`, `consecutive_failures`, `retry_after` и `run_history` определяют, что произошло с тикетом.
- `.vibe/runs/<run_id>/` — локальные артефакты конкретного запуска. Здесь лежат `run.json` с execution profile и идентичностью запуска, `events.jsonl` с сырым выводом `codex exec` и `result.json` со структурированным ответом агента. Каталоги `tickets/`, `runs/` и `tmp/` не должны попадать в коммиты code plane.

Как интерпретировать поля:

- `active_run` — только указатель на текущий незавершенный запуск. После завершения или ошибки поле очищается.
- `run_history[].run_id` — единый идентификатор запуска, одинаковый для тикета, prompt и каталога `.vibe/runs/<run_id>`.
- `run_history[].event` — durable timeline (`started`, `completed`, `failed`) для тикета; именно она нужна для ретроспективы после очистки `active_run`.
- `run_history[].artifacts_path` — относительный путь к локальным артефактам этого запуска.
- `prompt_path` и `prompt_version` в `run_history`/`run.json` — идентичность prompt-контракта конкретного запуска. `prompt_version` вычисляется как `sha256` от канонического prompt-контракта, сохраненного в `.vibe/runs/<run_id>/prompt.contract.txt` и `run.json["prompt_contract"]`: markdown prompt плюс execution-contract wrapper, placeholders runtime-полей и stage-specific execution profile.
- `model` и `reasoning_effort` в `run_history`/`run.json` — явная фиксация execution profile, с которым был выполнен конкретный запуск.
- `ticket_title`, `ticket_priority`, `ticket_parent`, `ticket_description` в `run_history`/`run.json` — durable snapshot mutable ticket-полей, которые реально были встроены в prompt этого запуска.

Практическое правило для расследований: сначала смотрите `run_history` в тикете как индекс запусков, затем открывайте `.vibe/runs/<run_id>/run.json` и `result.json`, и только после этого при необходимости углубляйтесь в `events.jsonl`.

## Конфигурация процессов

Процессы описываются декларативными YAML-файлами в `workflows/`. Промпты лежат в `prompts/`. Для прототипа это сделано намеренно; позднее файлового провайдера промптов можно будет заменить на версионируемый KMS-провайдер без изменения механики тикетов и процессов.

## Безопасность

Песочница Codex по умолчанию — `workspace-write`, а не `danger-full-access`. Оркестратор не коммитит файлы аутентификации Codex. Храните `.codex/auth.json` и другие учетные данные вне проектных репозиториев.

Это экспериментальный прототип локальной автоматизации. Запускайте его только на репозиториях, которые можно восстановить через Git.

## Известные ограничения прототипа

- Переходы, выполняемые человеком, намеренно упрощены: обычно кнопки UI следуют настроенному `next`; для Discovery `investment_decision` цель выбирается по `implementation_required`.
- Investment Decision сейчас моделирует только путь approve; ручные сценарии reject/correction вне агентных outcomes остаются следующей итерацией.
- `technical_analysis` создает Delivery-тикеты только из YAML-блока в `details`. `implementation_required: true` требует хотя бы один обязательный Delivery-тикет, а `implementation_required: false` требует пустой `delivery_tickets`; несогласованный результат возвращается на исправление. Дедупликация похожих тикетов пока не реализована.
- Для legacy Discovery-тикета без поля `implementation_required` на ручном переходе из `investment_decision` решение выводится из membership: наличие хотя бы одного Delivery-ребенка ведет в `implementation`, отсутствие — в `ready_for_validation`. Это режим совместимости, а не миграция данных; существующие YAML не переписываются автоматически.
- После входа в `implementation` scheduler gate ждет завершения только детей с `mandatory: true`; `done` означает завершенный Delivery-агрегат после release-интеграции. Необязательные дети и legacy-дети, если они помечены `mandatory: false`, не удерживают Discovery.
- Legacy Discovery YAML без `implementation_required` можно не переписывать: при переходе из `investment_decision` используется fallback по наличию Delivery-детей. Переход на явный контракт выполняется по одному тикету — после проверки membership добавьте boolean, не меняя `parent`, статусы и `run_history`; новые результаты `technical_analysis` уже должны содержать boolean.
- Traceability MVP хранит `run_history` в самом тикете и локальные артефакты в `.vibe/runs/`; централизованного аудиторского хранилища, retention policy и защиты от ручного редактирования YAML пока нет.
- Если процесс Codex завершается с ошибкой, тикет остается на активной стадии и занимает WIP во время ограниченной серии повторов; после исчерпания попыток требуется ручной повтор.
- UI намеренно минималистичен и не имеет зависимостей.
