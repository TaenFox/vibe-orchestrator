# vibe-orchestrator

Прототип pull-оркестратора на основе тикетов для локальных Codex CLI-агентов.

Оркестратор отвечает за **правила процесса и промпты**. Репозиторий целевого проекта отвечает за **тикеты, код, знания и рабочие артефакты**. Каждый запуск агента — это новый вызов `codex exec` для одного тикета на одной стадии.

## Текущая модель

### Термины и границы контракта

- **Тикет** — SQLite-агрегат control plane с одним текущим `status`; код, знания и
  артефакты реализации находятся в целевом репозитории и не становятся полями
  тикета.
- **Контекст тикета** (`context`) — актуальный структурированный handoff между
  стадиями с `context_revision`; `run_history` при этом остаётся неизменяемым
  аудитом запусков и содержит ссылки на ревизии контекста.
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
- **Бюджетный control plane** зафиксирован контрактом [`budget.v1`](BUDGETING.md),
  а Delivery runtime создаёт reservations и блокирует запуски по enforced-лимитам. В контракте `planned` — незарезервированные
  будущие obligations, `reserved` — active holds; они не учитываются дважды.
  `priority` — порядок планирования, а `wip` — лимит одновременно выполняемых
  обычных тикетов, не бюджет.

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
- Версионируемый контракт бюджетного control plane описан в [`BUDGETING.md`](BUDGETING.md);
  для ручных решений доступны `vibe budget increase-limit`, `allow-overrun`,
  `resolve-unknown` и `decisions`.

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

### Инструменты целевого репозитория

Оркестратор не поставляет инструменты контроля качества за целевой проект. Целевой
репозиторий обязан сам иметь необходимые зависимости, команды и тестовые fixtures для
проверки своего кода. Это особенно важно для UI: browser-level проверка возможна только
если в целевом репозитории установлен и настроен browser runner (например, Playwright),
а также есть команда запуска соответствующих тестов.

Доступные инструменты и ограничения должны быть явно описаны в корневом `AGENTS.md`
целевого репозитория. В нём следует указать команды тестов, дополнительные prerequisites,
доступные test runners и ограничения окружения. Агент обязан сверяться с этим файлом и не
выдавать статический анализ или запуск HTTP-сервера за browser-level проверку.

Если инструмент контроля отсутствует, это должно быть отражено как ограничение окружения.
Оркестратор не должен бесконечно создавать Rework только потому, что проект не объявил или
не установил необязательную capability; блокирующим замечанием может быть только дефект,
который доступные инструменты позволяют проверить.

### Функциональная документация

Владелец структуры функциональной документации — системный аналитик. В `context.documentation`
он указывает canonical path, режим (`create`, `update` или `not_required`), разделы и outline.
Если существующая функциональность ранее не была описана, системный аналитик сначала восстанавливает
в плане документации её baseline: назначение, пользовательские сценарии, состояния, ограничения
и ошибки. Разработчик затем обновляет этот документ фактическими изменениями тикета, не создавая
параллельную документацию и не реконструируя исходное поведение заново.

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

Для проектов с UI browser-level проверки подключаются отдельно, чтобы обычная
установка и запуск pytest не требовали Playwright или браузерных бинарников:

```bash
pip install -e '.[dev,browser]'
python -m playwright install chromium
pytest -m browser
```

Тесты, которым нужна эта capability, помечаются `@pytest.mark.browser`.
На этом этапе репозиторий предоставляет только opt-in контракт capability;
browser smoke-тесты добавляются отдельными тикетами.

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

Запустите UI на Starlette/Uvicorn:

```bash
vibe ui /path/to/your-project
```

Откройте `http://127.0.0.1:8765`. Переместите идею Discovery из **К выполнению** в **Готово**. `ready` — это очередь обязательств под контролем человека: агент никогда не забирает задачи напрямую из `todo`.

UI можно запустить вместе с оркестратором одной командой:

```bash
vibe run /path/to/your-project --ui
```

Для subprocess-проверок UI тесты предоставляют отдельную serial-фикстуру
`ui_server`. Она запускает переданную команду без shell-строки в новой POSIX
process group, передавая изолированный project/data root, `--host`, фактически
назначенный порт и `--no-browser` (последние параметры добавляет command factory).
Порт запрашивается через `0`, а после readiness доступен как `fixture.base_url`.
Ожидаемый HTTP-статус readiness настраивается параметром
`expected_readiness_status` и по умолчанию равен `200`; фактически наблюдённый
статус и ожидаемое значение сохраняются в metadata.
Обычный запуск остаётся serial и не требует parallel plugin. Параллельный режим
является только явным opt-in сценарием: каждый запуск получает собственные
artifact root, browser context, фактически bound port и state root.
Фактически bound port — это порт subprocess, который успешно прошёл readiness;
предварительный ephemeral-port probe не считается доказательством. Если subprocess
завершается с `EADDRINUSE`/`Address already in use`, fixture очищает только его
process group, выбирает новый кандидат и повторяет полный запуск не более
`port_attempts` раз. `manifest.json` сохраняет номер попытки, число retry, ошибки
конфликта и финальные `command`, `base_url`, `readiness_url` и `assigned_port`.

Пример:

```python
with ui_server(lambda project, host, port: [
    "vibe", "ui", str(project), "--host", host, "--port", str(port), "--no-browser"
]) as server:
    # HTTP/API checks use server.base_url.
    ...
```

### Browser artifacts and retention

`ui_server` создаёт run-каталог `.vibe/browser-artifacts/<test-id>/<run-id>/`.
В нём находятся `manifest.json`, `server.stdout.log`, `server.stderr.log`,
`state/` и `project/`. Manifest пишется через temporary file + replace и содержит
test/run ID, browser name/version (или null с reason), URL, фактический port,
worktree, roots, UTC timestamps, owned PID/PGID, доступные файлы и retention
status. При failure browser adapter сохраняет `screenshot.png` и `trace.zip`,
если capability доступна; для недоступных файлов сохраняется reason. Путь к
artifact root печатается в test output и в сообщении об ошибке; он совпадает с
`manifest.artifact_root`.

Политика по умолчанию — `retain-on-failure/delete-transient-on-success`:
после успешного teardown удаляются только transient logs/state текущего run,
а manifest остаётся как компактная запись cleanup. Для отладки можно явно
задать `BROWSER_ARTIFACT_RETENTION=always`; удаление ограничено текущим run root.

Метаданные также сохраняются в `server.diagnostics.metadata_path`, а полные
stdout и stderr — в `server.diagnostics.stdout_path` и
`server.diagnostics.stderr_path`. В metadata записываются PID, PGID, command,
host/port, URL, isolated root, readiness и состояние cleanup.
После обычного выхода сначала выполняется SIGTERM только собственной группе,
даже если root process уже завершился, затем при необходимости SIGKILL всей
оставшейся группе и её descendants. После teardown проверяется отсутствие root
и собственной process group; unrelated process не затрагивается. Missing runner, bind/start failure, startup
exit, readiness timeout и teardown failure имеют классификацию
`capability_environment_failure` и означают ограничение тестовой capability, а
не дефект UI. Browser-level DOM/focus/keyboard/viewport проверки в worker
окружении недоступны.

### Isolation invariants and parallel opt-in

Для двух явно созданных fixtures проверяются разные run/artifact roots, state
roots, browser contexts и ports; state одного run не виден другому. Это отдельное
opt-in доказательство и не включает parallel mode в обычный pytest запуск.
Browser cache/profile/state paths задаются на уровне run, а teardown idempotent и
ограничен собственной POSIX process group.
Для каждого run используются каталоги `state/browser-cache`,
`state/browser-profile` и `state/browser-state`; Playwright запускается через
отдельный persistent context с этим profile path. Исполняемый opt-in тест
запускает два fixture с overlapping lifetime, проверяет разные run/artifact/data/
state roots и ports, отвечает по двум разным marker URL и выполняет отрицательную
проверку cross-read/cross-write для state roots.

### Verification commands and environment limitations

Минимальные проверки: `python -m pytest --collect-only -q` и
`python -m pytest tests/test_browser_capability.py tests/test_ui_server_fixture.py -q`.
Проверка retry отдельно покрывает детерминированный `EADDRINUSE` и исчерпание
bounded попыток; при недоступном loopback она корректно пропускается как capability
тест.
Для browser capability нужны `pip install -e '.[dev,browser]'` и
`python -m playwright install chromium`. В текущем worker-контексте browser-level
DOM/focus/keyboard/viewport и реальная проверка parallel browser contexts не
подключены; это требует внешнего/manual runner. HTTP/API и статические тесты не
считаются browser-level доказательством.

Targeted проверка: `python -m pytest tests/test_ui_server_fixture.py -q`.

Новый UI использует серверный рендеринг и не требует отдельной сборки
frontend-пакетов. Доска, поиск, карточка тикета и создание тикетов работают
через framework routes; старый `http.server` больше не используется командами
`vibe ui` и `vibe run --ui`.

Доска, карточка и все операции изменения читаются из SQLite
`.vibe/control.sqlite3` и записываются транзакционно в него. Runtime больше не
читает и не создаёт YAML-файлы тикетов или сессий. Исторические YAML допустимы
только как вход одноразового мигратора и после миграции должны быть убраны из
рабочих каталогов.

В верхней части UI доступна форма создания тикетов Discovery, Delivery и Process Management. Поля `тип`, `заголовок`, `описание`, `приоритет` и `родительский ID` сохраняются сразу в SQLite.

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

Для планирования целостной Delivery-сессии используйте UI или CLI. Файл
`.vibe/tmp/delivery-session.yaml` больше не поддерживается:

```yaml
active: true
participants:
  - DEL-XXXXXX
  - DEL-YYYYYY
```

Пока сессия активна, в `system_analysis` проходят только перечисленные тикеты;
`wip_exempt`-тикеты (например, rework) сохраняют прежнее поведение. Состояние
сессии хранится в SQLite.

### Ограничения UI, telemetry и performance

UI проверяется регрессионными тестами для Discovery и Delivery: длинные заголовки
и описания экранируются и переносятся, `active_run` отражается на карточке и в
агрегате сессии, а API возвращает те же данные в JSON. Автообновление выполняется
раз в 8 секунд через `/fragment`, но не запускается при открытом drawer/details или фокусе в поле ввода;
это ограничение предотвращает потерю незавершенного текста.

Интерфейс рассчитан на локальную работу и не собирает telemetry: браузер не
отправляет события, метрики или содержимое полей во внешний сервис. Рендеринг
доски и endpoint `/api/tickets` читают локальную SQLite-базу на каждый запрос;
внутри одного render/request budget snapshots и индекс delivery-сессий переиспользуются,
но между запросами кэш не сохраняется. Полная страница содержит только shell drawer;
панель выбранного тикета загружается свежим GET `/drawer`.
После закрытия drawer fetched-панель удаляется, loading shell восстанавливается
для повторного открытия, а focus возвращается на кнопку-открыватель (если она
ещё доступна; при её удалении выполняется безопасный no-op). Поздний ответ
закрытого запроса не меняет DOM.

## Управляемые Delivery-сессии

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
├── control.sqlite3   # единственный runtime control plane
└── archive/          # исторические YAML и снятые runtime-артефакты
```

### Снимок control plane в SQLite

Для первоначального переноса runtime-состояния в базу используется только скрипт
миграции. Он читает исторические YAML тикетов и сессий, `run.json` и фактически применённые
`prompt.contract.txt`, а затем атомарно
пересобирает SQLite-снимок:

```bash
.venv/bin/python tools/migrate_control_plane.py . --dry-run
.venv/bin/python tools/migrate_control_plane.py .
```

По умолчанию база создаётся в `.vibe/control.sqlite3` и игнорируется Git.
Повторный запуск идемпотентен и используется только для первоначального импорта
или явного восстановления из архивного YAML-снимка. После миграции runtime
работает только с SQLite; обычные `TicketStore()` и `SessionStore()` не имеют
режима чтения или записи YAML.

В таблице `prompt_contracts` одинаковые применённые промты дедуплицируются по
SHA-256. Каждый запуск хранит ссылку на этот неизменяемый снимок через
`prompt_hash`; актуальный шаблон промта для будущих запусков будет отдельным
справочником на следующем этапе.

При DB-primary запуске `CodexRunner` сохраняет manifest, контракт, фактически
отрендеренный prompt, результат, события stdout и token usage непосредственно
в SQLite. Текстовые файлы внутри `.vibe/runs/<run_id>` для новых запусков не
создаются; временный файл output удаляется после чтения результата.

`result.json` импортируется в `run_results`, строки `events.jsonl` — в
`run_events`, а блок `token_usage` — в `token_usage`. Некорректные строки
`events.jsonl` пропускаются, исходные файлы при этом не изменяются.

Исторический YAML-снимок тикета выглядел так (новые тикеты так не хранятся):

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

Прототип намеренно **не** реализует базу данных, интеграцию с Jira, пользователей и полную permission-систему.

## Traceability и аудит

Source of truth для аудита разделен на два слоя:

- `.vibe/control.sqlite3` — локальное долговечное состояние control plane. Оно принадлежит checkout `stable`, не входит в code plane и не переносится merge-операциями между ветками. Поля `status`, `active_run`, `last_outcome`, `last_summary`, `consecutive_failures`, `retry_after` и `run_history` определяют, что произошло с тикетом.
- `.vibe/runs/<run_id>/` — локальные артефакты конкретного запуска. Здесь лежат `run.json` с execution profile и идентичностью запуска, `events.jsonl` с сырым выводом `codex exec` и `result.json` со структурированным ответом агента. Каталоги `tickets/`, `runs/` и `tmp/` не должны попадать в коммиты code plane.

Как интерпретировать поля:

- `active_run` — только указатель на текущий незавершенный запуск. После завершения или ошибки поле очищается.
- `run_history[].run_id` — единый идентификатор запуска, одинаковый для тикета, prompt и каталога `.vibe/runs/<run_id>`.
- Для `tech_debt_candidates.v1` source metadata считается согласованной только при наличии непустых строковых `ticket_id` и `stage`, их точном совпадении с source ticket/stage и совпадении `run_id == candidate.source_run`; это предотвращает принятие evidence из другого запуска.
- Если `.vibe/runs/<source_run>/run.json` существует, его metadata используется напрямую: даже пустой или неполный manifest не заменяется matching history и отклоняется с `TECH_DEBT_SOURCE_MISMATCH`.
- `run_history[].event` — durable timeline (`created`, `started`, `completed`, `failed`) для тикета; именно она нужна для ретроспективы после очистки `active_run`.
- `run_history[].ticket_type` — тип тикета, к которому относится событие.
- `run_history[].artifacts_path` — относительный путь к локальным артефактам этого запуска.
- `run_history[].source_artifact_path` или `source_artifacts` — исходный файл или каталог запуска; в read-only payload это нормализуется в объект `{path, links}`.
- `prompt_path` и `prompt_version` в `run_history`/`run.json` — идентичность prompt-контракта конкретного запуска. `prompt_version` вычисляется как `sha256` от канонического prompt-контракта, сохраненного в `.vibe/runs/<run_id>/prompt.contract.txt` и `run.json["prompt_contract"]`: markdown prompt плюс execution-contract wrapper, placeholders runtime-полей и stage-specific execution profile.
- `model` и `reasoning_effort` в `run_history`/`run.json` — явная фиксация execution profile, с которым был выполнен конкретный запуск.
- `ticket_title`, `ticket_priority`, `ticket_parent`, `ticket_description` в `run_history`/`run.json` — durable snapshot mutable ticket-полей, которые реально были встроены в prompt этого запуска.
- `audit_events[]` — append-only журнал успешных `create_ticket`/`update_ticket`; при миграции отсутствующее поле трактуется как пустой список.

### Write tools

Агенты изменяют тикеты только через `create_ticket`/`update_ticket` из
`agent_tools` либо через `POST /api/agent/tickets` и `PATCH
/api/agent/tickets/<id>`. Разрешены только metadata-поля; неизвестные и
lifecycle-поля отвергаются целиком. Все новые тикеты получают
`workflow.initial_status` (для Delivery — `todo`). `priority` — целое число от
нуля, title обрезается по краям, parent/blocked_by проверяются на существование,
совместимость, self-reference и циклы.

Успешные операции сохраняют actor, origin, before/after, changed_fields и
timestamp в audit event. `expected_updated_at` защищает от stale update,
а `idempotency_key` делает повтор create безопасным: тот же payload возвращает
исходный тикет без нового события, другой payload дает conflict. Delivery
tech-debt в текущей модели представляется как обычный `task`; агент не может
сразу выбрать queue или agent status. Для validated basis safe `create_ticket`
вычисляет `tech_debt.v1:<sha256>` из консервативно нормализованных problem,
suggested_scope и evidence; basis сохраняется immutable. Поиск ограничен
активными Delivery story/task/bug: exact возвращает существующий тикет без
нового audit event, ambiguous возвращает стабильных кандидатов, done и legacy
тикеты без basis не блокируют создание.

При replay `technical_analysis` с тем же canonical tech-debt key exact active
ticket остается неизменным: сохраняются его parent, status, metadata,
`audit_events` и SQLite-представление. Такой ticket считается результатом текущей
reconciliation по своему ID и не деактивируется, даже если его нет в обычном
`delivery_tickets` snapshot. Повтор с тем же `source_run` сохраняет ровно один
active matching ticket.

При `create_ticket` новый тикет сначала полностью формируется в памяти: в него
попадают `run_history.created` и обязательное событие `ticket_created`, после
чего выполняется одна транзакция SQLite. Это гарантия атомарности одной записи
control plane, а не `fsync`-гарантия после отключения питания.

Практическое правило для расследований: сначала смотрите `run_history` в тикете как индекс запусков, затем открывайте `.vibe/runs/<run_id>/run.json` и `result.json`, и только после этого при необходимости углубляйтесь в `events.jsonl`.

## Контракт технического долга

### Baseline: назначение и сквозной сценарий

Технический долг — независимая Delivery-задача из результата Discovery
`technical_analysis`. Она фиксирует проблему, evidence и scope, но не является
обязательным ребенком Discovery-идеи и не запускается автоматически в текущей
Delivery-сессии. Тип задачи всегда `task`; `source_ticket`, `source_stage` и
`source_run` сохраняются в `context.origin`.

Сценарий: агент возвращает необязательный YAML `tech_debt_candidates`; оркестратор
валидирует весь список и read-only проверяет source/evidence; затем вычисляет
dedup key и либо возвращает exact/ambiguous результат, либо создает независимый
тикет. Новый тикет получает `delivery/todo`, `parent: null`, `mandatory: false`,
`blocked_by: []`, `technical_debt_deferred: true` и audit-событие создания.
Человек добавляет его в draft Delivery-сессию и переводит `todo` в
`selected_for_session`; после активации scheduler может выбрать его в
`system_analysis`. Без active-сессии deferred-тree не запускается и не блокирует
другие тикеты.

### Формат кандидата и проверки

Контракт имеет `version: tech_debt_candidates.v1` и `candidates: []`. Каждый
кандидат обязан содержать ровно `problem`, `evidence`, `impact`, `suggested_scope`,
`source_ticket`, `source_stage`, `source_run`, `type`, `urgency`, `priority`.
`evidence` содержит ровно `path`, `identifier`, `observation`; строки непустые
после trim, `type=task`, `urgency` — `low|medium|high`, `priority` — целое
неотрицательное число. Отсутствие корневого блока означает пустой список.

`source_ticket` должен существовать, `source_stage` — быть стадией его workflow,
а `source_run` — совпадать с `run_history` либо `.vibe/runs/<source_run>/run.json`.
Если manifest существует, он является источником истины и неполные metadata не
подменяются history; проверяются точные `ticket_id`, `stage` и `run_id`. Evidence
должен быть файлом внутри project root, не `.vibe/archive/**`, не
`.vibe/runs/**` и не каталогом; `identifier` ищется в файле, observation
проходит verifier. Ошибка preflight не создает частичный набор задач.

Defaults materialization: `status=todo`, `process=delivery`, `type=task`,
`parent=null`, `mandatory=false`, `blocked_by=[]`,
`technical_debt_deferred=true`; `priority` берется из кандидата. `origin` имеет
вид `technical_analysis:<source_run>`, а `context` содержит problem, evidence,
impact, suggested_scope и immutable origin.

### Deduplication и lifecycle

Basis строится из NFC-normalized, trimmed, whitespace-collapsed, case-folded
`problem`, `suggested_scope` (в basis `area`) и `evidence.path/identifier/observation`.
Ключ — `tech_debt.v1:<sha256(canonical-json)>`; basis сохраняется рядом с ним.
Поиск идет только по незавершенным Delivery `story/task/bug` с тем же key: ноль
совпадений создает тикет, одно возвращает `exact`, два и более возвращают
`ambiguous` без mutation. `done` и legacy без basis не препятствуют созданию.
Exact replay сохраняется в reconciliation и не деактивируется.

Переход `todo -> selected_for_session` human-controlled. При active Delivery
сессии в `system_analysis` проходят только effective members; deferred ticket вне
состава остается без запуска. Без active-сессии обычные legacy selected-текеты
сохраняют совместимость, но `technical_debt_deferred=true` всегда исключается.
После membership gate применяются обычные проверки WIP, budget, `active_run`,
`blocked_by` и retry/backoff.

### Agent tools, allowlist и audit

Read API (`agent.read.v1`): `list_tickets`, `get_ticket`, `list_sessions`,
`get_session`; pagination и history limits bounded, artifact links не выходят из
`.vibe/runs`. Write API: `create_ticket`/`update_ticket` (`agent.write.v1`) и
draft-session membership `add/remove/update` (`agent.session.write.v1`). Session
lifecycle, scheduler transitions и materialization tech-debt — privileged.

Create allowlist: `process`, `type`, `title`, `description`, `priority`, `parent`,
`mandatory`, `idempotency_key`, `origin`, initial-only `status`, validated
`technical_debt`. Update allowlist: `title`, `description`, `priority`, `parent`,
`blocked_by`, `mandatory`, `context`, `origin`, `expected_updated_at`.
Lifecycle/identity fields (`id`, `process`, `type`, `status`, `active_run`,
`run_history`, outcomes, timestamps, `wip_exempt`, rework/correction metadata)
запрещены. `origin` и actor дают traceability, но не authorization.

Успешные writes добавляют append-only `audit_events[]` с operation, actor, origin,
ticket ID, timestamp, sorted changed fields, before/after и при необходимости
idempotency key. Exact replay не создает новое событие; stale expected timestamp,
conflicting idempotency и ambiguous dedup возвращают conflict/result без mutation.

### Ошибки и ограничения MVP

Ошибки имеют envelope `orchestrator.errors.v1` и path; основные коды:
`TECH_DEBT_INVALID`, `TECH_DEBT_SOURCE_NOT_FOUND`, `TECH_DEBT_SOURCE_MISMATCH`,
`TECH_DEBT_PREFLIGHT_UNAVAILABLE`, `TECH_DEBT_MUTATION_BLOCKED`. Read-only операции
не создают миграции, каталоги или кэш. Runtime хранит control plane в SQLite,
runs и бинарные артефакты — в файловой системе;
транзакция не является fsync-гарантией после отключения питания, audit не защищен от
ручного редактирования, внешней authorization/DB/Jira и budget enforcement нет.

## Read-only agent queries

Read-only tools позволяют агентам получать тикеты и delivery-сессии без изменения
control-plane состояния. `list_tickets` поддерживает фильтры `process`, `status`,
`parent`, `session`, пагинацию `offset`/`limit` и `history_limit`; `get_ticket`
возвращает полную модель тикета с ограниченной историей запусков и признаком
`run_history_truncated`. `list_sessions` и `get_session` возвращают `status`,
`participants`, `audit_events` и `effective_membership`. Те же данные доступны
через `/api/agent/tickets`, `/api/agent/tickets/<id>`, `/api/agent/sessions` и
`/api/agent/sessions/<id>`.

В `run_history` поля `artifacts` и `source_artifacts` имеют форму `{path, links}`.
Ссылки строятся только для существующих файлов внутри `.vibe/runs/<run_id>` и
ведут на `/artifacts/<run_id>/<file>` с безопасным кодированием сегментов пути.
Если `artifacts_path` указывает на файл, ссылка сохраняет его относительный путь
от `.vibe/runs/<run_id>`, включая имя файла; если он указывает на каталог, ссылки
по-прежнему перечисляют файлы относительно этого каталога.
Для source artifacts `run_id` принимается только как имя одного каталога
непосредственно под canonical `.vibe/runs`; traversal, абсолютные значения,
разделители и symlink-каталоги наружу отклоняются.
Невалидные, отсутствующие или внешние source paths дают пустой `links` без ошибки.
При наличии непустого `source_artifacts` он имеет приоритет над
`source_artifact_path`. Запросы используют положительные integer limits с верхними
bounds, не вызывают init/save/migration и не меняют ticket YAML.

## Write tools / Agent write boundary

Агентский контракт `agent.session.write.v1` добавляет только безопасные операции
составом draft-сессии: `add_to_session`, `remove_from_session` и
`update_session_membership`. Они требуют непустые `actor` и `origin`, проверяют
существование Delivery story/task/bug/rework, отсутствие дублей и конфликтов с
другой открытой сессией. Завершённые тикеты, тикеты с незавершёнными
`blocked_by` и неизвестными dependency отвергаются; зависимости и lifecycle
тикета при этом не изменяются.

`update_session_membership` атомарно заменяет полный состав. Элементы имеют
`ticket_id`, необязательные уникальные неотрицательные `position` и `priority`;
порядок сохраняется в `ticket_ids`, а `membership_priorities` хранится отдельно
от `Ticket.priority` (при миграции отсутствие этого поля заменяется на default `100`).
Успешная запись добавляет append-only audit event с `actor`, `origin`, временем,
`before`, `after` и `changed_fields`; повтор без изменений события не создаёт.
Запись выполняется транзакционно в SQLite под процессным lock, поэтому ошибка
валидации не оставляет частичного состава.

Агенты не получают `activate`, `complete` или `cancel` и не могут менять active,
completed или cancelled session. Автоматический Delivery rework по-прежнему
наследуется в active session через privileged путь оркестратора; ручные write
tools этот путь не вызывают и active membership не редактируют.

Операции доступны Python-обёртками в `AgentSessionTools` и transport-маршрутом
`/api/agent/sessions/<id>/add`, `/remove` и `/membership` (PATCH или POST).

## Корректирующая работа

Rework, созданный оркестратором для Delivery-родителя, наследует активную
сессию через `SessionStore.inherit_ticket`; повторное наследование идемпотентно.
Агентские tools состава не создают rework и не могут вручную расширить active
сессию, поэтому корректирующая работа не выпадает из scheduler membership.

## Конфигурация процессов

Процессы описываются декларативными YAML-файлами в `workflows/`. Промпты лежат в
`prompts/`. Это конфигурация приложения, а не хранение состояния тикетов и
сессий.

## Безопасность

Песочница Codex по умолчанию — `workspace-write`, а не `danger-full-access`.
Агентский contract не предоставляет `TicketStore.save` и прямую запись control
plane; lifecycle остается у UI и оркестратора. `origin` фиксирует
источник операции, но не заменяет авторизацию. Оркестратор не коммитит файлы
аутентификации Codex. Храните `.codex/auth.json` и другие учетные данные вне
проектных репозиториев.

Это экспериментальный прототип локальной автоматизации. Запускайте его только на репозиториях, которые можно восстановить через Git.

## Известные ограничения прототипа

HTTP/API contract покрыт unit/API тестами; browser-level проверка DOM, focus,
keyboard и viewport в worker-контексте недоступна и требует ручного или внешнего
прогона.

## Ограничения UI, telemetry и performance

UI не является telemetry или billing системой: budget read-model читается из
локального SQLite и не содержит цены. Повторные budget reads в одном API/render
проходе переиспользуют bounded request-local snapshots по scope; cache не
переживает запрос. Пагинация и внешний provider API не
реализованы. Browser-level smoke выполняется вручную, поскольку worker-контекст
не подключает browser runner.

Benchmark сравнивает одинаковые dataset, storage, seed, warmup и iterations.
Degradation >5% считается regression signal, improvement >=20% — optimization
candidate; абсолютный latency SLO не установлен.

## Budget control plane

Delivery API `/api/tickets` и `/api/sessions` публикует read-only `budget` и
`budget_runs` с limits, spent, reserved, available, lifecycle counts/status,
confidence и fresh/stale/unavailable metadata. Card, drawer и session panel
показывают тот же контракт; missing legacy records не превращаются в unlimited.
Полный контракт и manual browser smoke описаны в [BUDGETING.md](BUDGETING.md).

- Переходы, выполняемые человеком, намеренно упрощены: обычно кнопки UI следуют настроенному `next`; для Discovery `investment_decision` цель выбирается по `implementation_required`.
- Investment Decision сейчас моделирует только путь approve; ручные сценарии reject/correction вне агентных outcomes остаются следующей итерацией.
- `technical_analysis` создает Delivery-тикеты только из YAML-блока в `details`. `implementation_required: true` требует хотя бы один обязательный Delivery-тикет, а `implementation_required: false` требует пустой `delivery_tickets`; несогласованный результат возвращается на исправление. Tech-debt candidates после preflight используют тот же safe write boundary и find-or-create.
- В том же верхнеуровневом YAML `details` можно передать `tech_debt_candidates` версии `tech_debt_candidates.v1`. Каждый кандидат обязан содержать непустые `problem`, `impact`, `suggested_scope`, `source_ticket`, `source_stage`, `source_run`, `type: task`, `urgency: low|medium|high`, неотрицательный целочисленный `priority` и `evidence` с `path`, `identifier`, `observation`. Отсутствующий ключ означает пустой список; неизвестная версия, поле или malformed payload — ошибка.
- Preflight отклоняет кандидата с отсутствующим или отличающимся `run.json.run_id`, а также с отсутствующими, нестроковыми, пустыми или whitespace-only `ticket_id`/`stage`, ошибкой `TECH_DEBT_SOURCE_MISMATCH` по пути `tech_debt_candidates.candidates[N].source_run`; при этом source ticket, session и Delivery children не изменяются.
- Dedup key не включает source_run, source_ticket, urgency, priority, actor или origin; scan, ambiguity decision и транзакционная запись защищены межпроцессным lock-файлом. Ошибка чтения/lock не создаёт тикет.
- При отсутствии manifest допускается legacy fallback на последнюю matching-запись `run_history`, но выбранная запись обязана содержать непустые строковые `ticket_id` и `stage`, точно совпадающие с source ticket/source stage, а `run_id` — с `source_run` кандидата. Неполная identity-запись отклоняется без попытки использовать другую history-запись.
- Accepted manifest metadata: `{"run_id":"run-1","ticket_id":"DISC-ABC123","stage":"technical_analysis"}` при соответствующих `source_run`, source ticket и source stage кандидата. Rejected: `{ "run_id": "run-1" }`, `ticket_id: null`, `stage: "   "` или любое нестроковое значение; matching history не используется, если такой `run.json` уже существует.
- Перед любым созданием Delivery-тикета выполняется read-only preflight: проверяются source ticket, stage, run artifact, согласованность run metadata, активные Delivery-сессии и безопасный путь evidence. Единый error envelope имеет `contract_version: orchestrator.errors.v1`, `code`, `path`, `message`; ошибки `TECH_DEBT_INVALID`, `TECH_DEBT_SOURCE_NOT_FOUND`, `TECH_DEBT_SOURCE_MISMATCH` и `TECH_DEBT_PREFLIGHT_UNAVAILABLE` блокируют mutation. Такие contract errors не являются `needs_correction`: source ticket и session остаются без изменений, `_record_failure` и follow-up не вызываются, corrective или Delivery children не создаются. Кандидат автоматически в сессию не добавляется.
- Созданная tech-debt задача помечается внутренним scheduler-маркером `technical_debt_deferred: true`. Пока нет активной Delivery-сессии, этот маркер исключает задачу из автоматического планирования даже при ручном переводе в `selected_for_session`; обычные legacy-тикеты сохраняют прежнее поведение. При наличии активной сессии задача допускается только как ее effective member, а budget/WIP-проверки выполняются до запуска.
- Для legacy Discovery-тикета без поля `implementation_required` на ручном переходе из `investment_decision` решение выводится из membership: наличие хотя бы одного Delivery-ребенка ведет в `implementation`, отсутствие — в `ready_for_validation`. Это режим совместимости, а не миграция данных; существующие YAML не переписываются автоматически.
- После входа в `implementation` scheduler gate ждет завершения только детей с `mandatory: true`; `done` означает завершенный Delivery-агрегат после release-интеграции. Необязательные дети и legacy-дети, если они помечены `mandatory: false`, не удерживают Discovery.
- Legacy Discovery YAML без `implementation_required` можно не переписывать: при переходе из `investment_decision` используется fallback по наличию Delivery-детей. Переход на явный контракт выполняется по одному тикету — после проверки membership добавьте boolean, не меняя `parent`, статусы и `run_history`; новые результаты `technical_analysis` уже должны содержать boolean.
- Traceability MVP хранит `run_history` в самом тикете и локальные артефакты в `.vibe/runs/`; централизованного аудиторского хранилища, retention policy и защиты от ручного редактирования YAML пока нет. Lock защищает локальные процессы, но не заменяет транзакционную БД или ручное разрешение legacy-дублей.
- Если процесс Codex завершается с ошибкой, тикет остается на активной стадии и занимает WIP во время ограниченной серии повторов; после исчерпания попыток требуется ручной повтор.
- UI намеренно минималистичен и не имеет зависимостей.
- Для `evidence.observation` preflight требует явно переданную read-only capability `observation_verifier(content, identifier, observation) -> bool`. Отсутствие capability или ошибка/недопустимый результат verifier возвращает `TECH_DEBT_PREFLIGHT_UNAVAILABLE`; отрицательный результат — `TECH_DEBT_SOURCE_MISMATCH`. Непустая строка observation и наличие identifier сами по себе доказательством не являются. Browser-level проверки DOM/focus/viewport для этого контракта не требуются и в worker-контексте недоступны.
