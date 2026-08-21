# Инструменты агентов

Этот репозиторий выполняется локальными агентами оркестратора через `codex exec`.

## Доступно

- shell-команды в рабочем дереве тикета;
- Git и отдельный worktree тикета;
- Python 3.11 и зависимости проекта;
- тесты через `.venv/bin/pytest`;
- запуск локального UI для ручной проверки.

## Browser capability contract

Browser-level проверка — отдельный optional test class. Для non-UI проектов и
static/API-only тикетов она не обязательна; достаточно релевантных проверок и
явной записи `browser-not-applicable`. В текущем worker browser automation и
browser-level runner недоступны, поэтому для DOM, focus, keyboard, viewport,
timing или real interaction behavior нужен внешний/manual browser-enabled run.

### Prerequisites и команды

```bash
python -m pytest -q                    # static и API/HTTP
python -m pytest --collect-only -q     # collection, не browser evidence
pip install -e '.[dev,browser]'
python -m playwright install chromium
python -m pytest -m browser tests/browser/test_ui_smoke.py -q
```

Обычная команда не требует Playwright/Chromium. Browser tests помечаются
`@pytest.mark.browser`; marker-команда является evidence только если runner
реально стартовал браузер и тесты завершились результатом, а не skip.

### Evidence, artifacts и безопасность

Static evidence — unit/source inspection, JS harness и HTML/string assertions;
API/HTTP evidence — endpoint/readiness checks. Browser-level evidence требует
реального браузера, DOM/accessible tree или interaction observation и
сохранённого result/artifact. Browser claim без фактического artifact/result
недопустим.

Artifact root: `.vibe/browser-artifacts/<test-id>/<run-id>/`, обязательный
`manifest.json`; diagnostics: `server.stdout.log`, `server.stderr.log`, URL,
assigned port, roots, PID/PGID, timestamps, outcome и cleanup status. При
доступном браузере failure evidence может включать `screenshot.png` и
`trace.zip`; при недоступности reason сохраняется в manifest. Default retention:
`retain-on-failure/delete-transient-on-success`; override:
`BROWSER_ARTIFACT_RETENTION=always`. Cleanup удаляет только пути внутри run
root, safety failure сохраняет evidence. Artifacts не добавляются в Git.

Не записывайте секреты, токены, пароли и ключи в manifest, logs, screenshots,
traces или handoff; environment keys с `SECRET`, `TOKEN`, `PASSWORD` или `KEY`
показывайте как `<redacted>`. В документации используйте template path, не
machine-specific absolute path.

### Semantic locators

Предпочитайте `get_by_role` с accessible name, `get_by_label`,
`get_by_text(..., exact=True)` для видимого текста и domain `data-*`, например
`data-ticket` и `data-drawer-ticket`. `nth-child`, позиционные/layout-only и
class-only selectors, а также XPath по внутренней структуре запрещены как
primary contract: locator должен выражать role, name, label или domain identity.

## Ограничения

- Browser automation и browser-level test runner в worker-контексте не подключены.
- Нельзя считать запуск локального HTTP-сервера или проверку HTML-строк browser-level проверкой.
- Если тикет требует проверки реального DOM, фокуса, клавиатуры, viewport или автообновления в браузере,
  такую проверку нужно явно отметить как недоступную в текущем контексте и описать необходимый ручной
  или внешний прогон. Отсутствие инструмента не является дефектом реализации и не блокирует тикет.
- Не заявляйте browser-level покрытие только на основании статических тестов или анализа исходников.
- Ошибка assertion после успешного browser startup — product finding; отсутствие
  runner, Chromium, startup/readiness или teardown — capability limitation, а не
  product defect.

## Правило handoff

В результате работы указывайте выполненные тесты отдельно от недоступных browser-сценариев.
Если browser-level проверка недоступна, зафиксируйте это как ограничение окружения и не возвращайте
`needs_rework` только по этой причине. Возвращайте `needs_rework` лишь для проблем, которые можно
проверить доступными инструментами или по имеющимся артефактам.
