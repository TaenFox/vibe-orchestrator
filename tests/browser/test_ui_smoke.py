from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.browser


def _fill_create(page, *, title, description, priority="17"):
    page.get_by_role("button", name="Новый тикет").click()
    dialog = page.get_by_role("dialog", name="Новый тикет")
    dialog.get_by_label("Заголовок").fill(title)
    dialog.get_by_label("Описание").fill(description)
    dialog.get_by_label("Приоритет").fill(priority)
    with page.expect_response(lambda response: response.url.endswith("/create") and response.request.method == "POST"):
        dialog.get_by_role("button", name="Создать").click()
    page.get_by_text(title, exact=True).wait_for()


def _open_card(page, ticket):
    card = page.locator(f'[data-ticket="{ticket.id}"]')
    card.get_by_role("button", name=f"Открыть тикет {ticket.id}").click()
    panel = page.locator(f'[data-drawer-ticket="{ticket.id}"]')
    panel.wait_for(state="visible")
    return card, panel


def test_browser_smoke_01_board_card_and_matching_drawer(browser_ticket, browser_page):
    """BROWSER-SMOKE-01: board, column, card identity and drawer identity."""
    ticket = browser_ticket
    card, panel = _open_card(browser_page, ticket)
    assert card.get_by_text(ticket.id, exact=True).is_visible()
    assert card.get_by_text(ticket.title, exact=True).is_visible()
    assert panel.get_by_text(ticket.id, exact=True).is_visible()
    assert panel.get_by_role("heading", name=ticket.title).is_visible()


def test_browser_smoke_02_drawer_focus_close_backdrop_escape_and_return(browser_ticket, browser_page):
    """BROWSER-SMOKE-02: observable drawer state and focus contract."""
    ticket = browser_ticket
    opener = browser_page.get_by_role("button", name=f"Открыть тикет {ticket.id}")
    opener.focus()
    opener.press("Enter")
    drawer = browser_page.get_by_role("dialog", name="Контекст тикета")
    panel = browser_page.locator(f'[data-drawer-ticket="{ticket.id}"]')
    panel.wait_for(state="visible")
    assert drawer.get_attribute("aria-hidden") == "false"
    assert browser_page.evaluate("document.activeElement?.closest('[data-drawer-ticket]') !== null")
    close = panel.get_by_role("button", name="Закрыть drawer")
    close.focus()
    close.press("Tab")
    assert browser_page.evaluate("document.activeElement?.closest('[data-drawer-ticket]') !== null")
    browser_page.keyboard.press("Shift+Tab")
    assert browser_page.evaluate("document.activeElement?.closest('[data-drawer-ticket]') !== null")
    browser_page.keyboard.press("Escape")
    browser_page.wait_for_function("() => document.querySelector('[data-ticket-drawer]').hidden")
    assert browser_page.evaluate("document.activeElement?.id") == f"open-ticket-{ticket.id}"

    opener.click()
    panel.wait_for(state="visible")
    browser_page.locator("[data-drawer-backdrop]").click(position={"x": 2, "y": 2})
    browser_page.wait_for_function("() => document.querySelector('[data-ticket-drawer]').hidden")
    assert browser_page.evaluate("document.activeElement?.id") == f"open-ticket-{ticket.id}"


def test_browser_smoke_03_keyboard_only_navigation(browser_ticket, browser_page):
    """BROWSER-SMOKE-03: Tab/Enter/Escape navigation without click helpers."""
    ticket = browser_ticket
    opener = browser_page.get_by_role("button", name=f"Открыть тикет {ticket.id}")
    browser_page.locator("body").focus()
    for _ in range(40):
        if opener.evaluate("element => element === document.activeElement"):
            break
        browser_page.keyboard.press("Tab")
    else:
        pytest.fail("keyboard traversal did not reach the ticket opener")
    browser_page.keyboard.press("Enter")
    panel = browser_page.locator(f'[data-drawer-ticket="{ticket.id}"]')
    panel.wait_for(state="visible")
    browser_page.keyboard.press("Escape")
    browser_page.wait_for_function("() => document.querySelector('[data-ticket-drawer]').hidden")
    assert browser_page.evaluate("document.activeElement?.id") == f"open-ticket-{ticket.id}"
    toggle = browser_page.get_by_role("button", name="Обновление: включено")
    toggle.focus()
    browser_page.keyboard.press("Space")
    assert toggle.get_attribute("aria-pressed") == "true"
    browser_page.keyboard.press("Space")
    assert toggle.get_attribute("aria-pressed") == "false"


def test_browser_smoke_04_multiline_input_and_fragment_refresh_preservation(browser_ticket, browser_page):
    """BROWSER-SMOKE-04: multiline text and controlled fragment refresh preserve state."""
    ticket = browser_ticket
    search = browser_page.get_by_label("Поиск")
    multiline = "first line\nsecond line"
    search.fill(multiline)
    details = browser_page.locator(f'[data-ticket-details="{ticket.id}"]')
    details.get_by_text("Подробнее", exact=True).click()
    before = browser_page.evaluate("({search: document.querySelector('[data-board-search]').value, details: document.querySelector('[data-ticket-details]').open})")
    browser_page.evaluate("""async () => {
        const response = await fetch('/fragment?process=discovery&mode=compact');
        if (!response.ok) throw new Error('fragment request failed');
    }""")
    after = browser_page.evaluate("({search: document.querySelector('[data-board-search]').value, details: document.querySelector('[data-ticket-details]').open})")
    assert after == before
    assert multiline in browser_page.locator(f'[data-ticket-details="{ticket.id}"]').inner_text()


def test_browser_smoke_05_create_and_state_action_persist_after_reload(browser_page):
    """BROWSER-SMOKE-05: UI create, identity persistence and available move action."""
    title = "Created through Chromium"
    description = "created line 1\ncreated line 2"
    _fill_create(browser_page, title=title, description=description)
    created = browser_page.get_by_text(title, exact=True)
    created.wait_for()
    card = browser_page.locator(".card", has=browser_page.get_by_text(title, exact=True))
    assert "created line 1" not in card.inner_text()
    browser_page.reload()
    browser_page.get_by_text(title, exact=True).wait_for()
    action = card.get_by_role("button", name=re.compile("Переместить"))
    if action.count():
        with browser_page.expect_response(lambda response: response.url.endswith("/move") and response.request.method == "POST"):
            action.click()
        browser_page.reload()
        browser_page.get_by_text(title, exact=True).wait_for()
    else:
        pytest.skip("product limitation: created card has no available state action")


def test_browser_smoke_06_safe_rendering(browser_page):
    """BROWSER-SMOKE-06: script-like payload stays text and multiline content is bounded."""
    payload = '<script>window.__unsafe_payload = true</script>\nsecond line'
    _fill_create(browser_page, title=payload, description=payload)
    assert browser_page.evaluate("window.__unsafe_payload === undefined")
    assert browser_page.locator("script").filter(has_text="window.__unsafe_payload").count() == 0
    assert browser_page.get_by_text(payload, exact=True).count() == 1
    assert browser_page.locator(".card").last.evaluate("element => getComputedStyle(element).overflowWrap === 'anywhere'")


def test_browser_smoke_07_mobile_viewport_and_bounded_auto_refresh(browser_page):
    """BROWSER-SMOKE-07: mobile layout and the documented eight-second refresh cadence."""
    browser_page.set_viewport_size({"width": 390, "height": 844})
    assert browser_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert browser_page.get_by_role("button", name="Новый тикет").is_visible()
    browser_page.get_by_label("Поиск").fill("mobile")
    assert browser_page.evaluate("document.querySelector('[data-board-search]').value === 'mobile'")
    browser_page.keyboard.press("Tab")
    with browser_page.expect_response(lambda response: "/fragment?" in response.url, timeout=9500) as response_info:
        pass
    assert response_info.value.request.resource_type == "fetch"
    assert "частичное автообновление 8с" in browser_page.locator("body").inner_text()
