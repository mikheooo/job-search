"""T7.26.2 — the SERP selectors, held to the live DOM.

T7.26.1 found SEARCH_ONLY blind: 34 query runs, **0 cards**, because
``COUNT_RESULTS_JS`` and ``READ_CARDS_JS`` both looked for
``[data-qa="serp-item"]``, an attribute the live board no longer renders.

The old test suite could not have caught it. It scripted the session
(``evaluate_on`` returned canned cards whenever the expression *was*
``READ_CARDS_JS``), so no selector was ever evaluated against a DOM — the mock
matched by identity, and identity is exactly what a stale selector preserves.

So these tests split in two:

* the **contract** half is cheap and browser-free: the live attribute must be the
  one that has to match, and both scripts must be *built from* the same constant,
  so a stale copy cannot reappear in one of them;
* the **DOM** half is the real thing: a real browser, a real document holding the
  live card structure, and the production scripts evaluated against it. It carries
  a negative control — the old selector must find **nothing** — because a test
  that only ever passes proves nothing about the defect it was written for.

The DOM half skips (not fails) where chromium is unavailable: it is a regression
net, not a reason to break an unrelated machine.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant import hh_readonly_operation as ro

LIVE_CONTAINER = '[data-qa="vacancy-serp__vacancy"]'
LEGACY_CONTAINER = '[data-qa="serp-item"]'

# The structure measured live on 2026-09-30 (tools/.t7262/salary_dom_probe.py):
#   * the card root carries data-qa="vacancy-serp__vacancy";
#   * the title link still carries data-qa="serp-item__title" - a *different*
#     value, which is the trap: an exact-match query for "serp-item" misses it;
#   * the salary is the first direct child <span> of the compensation-labels block
#     and carries **no** data-qa; the only data-qa in that block belongs to the
#     payment-frequency note.
#   * a card without a salary has a <div> tag as that first child instead.
SERP_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body>
<div data-qa="vacancy-serp__vacancy">
  <a data-qa="serp-item__title" href="https://hh.ru/vacancy/111111?from=serp&amp;hhtmFrom=vacancy_search_list">
    <span data-qa="serp-item__title-text">Инженер технической поддержки</span>
  </a>
  <div class="compensation-labels--vwum2s12fQUurc2J compensation-labels_magritte--pbBIkJ7Ww24Z">
    <span class="magritte-text___pbpft_5-3-12_xhh magritte-text_style-primary___AQ7MW_5-3-12_xhh">от&nbsp;<data value="90000">90 000</data>&nbsp;<data value="RUB">₽</data>&nbsp;за&nbsp;месяц, на руки</span>
    <div class="magritte-tag___WdGxk"><div class="magritte-tag__label___YHV-o"><data value="1-3" data-qa="vacancy-serp__vacancy-work-experience-between1And3">Опыт 1-3 года</data></div></div>
    <div class="magritte-tag___WdGxk"><div class="magritte-tag__label___YHV-o"><span data-qa="vacancy-serp__vacancy-compensation-frequency-TWICE_PER_MONTH">Выплаты: Два раза в&nbsp;месяц</span></div></div>
  </div>
  <a data-qa="vacancy-serp__vacancy-employer" href="/employer/1"><span data-qa="vacancy-serp__vacancy-employer-text">ООО Ромашка</span></a>
  <span data-qa="vacancy-serp__vacancy-address">Москва</span>
</div>
<div data-qa="vacancy-serp__vacancy">
  <a data-qa="serp-item__title" href="https://hh.ru/vacancy/222222?from=serp&amp;hhtmFrom=vacancy_search_list">
    <span data-qa="serp-item__title-text">Системный администратор</span>
  </a>
  <div class="compensation-labels--vwum2s12fQUurc2J compensation-labels_magritte--pbBIkJ7Ww24Z">
    <div class="magritte-tag___WdGxk"><div class="magritte-tag__label___YHV-o"><data value="3-6" data-qa="vacancy-serp__vacancy-work-experience-between3And6">Опыт 3-6 лет</data></div></div>
    <div class="magritte-tag___WdGxk"><div class="magritte-tag__label___YHV-o"><span data-qa="vacancy-label-work-schedule-remote">Можно удалённо</span></div></div>
  </div>
  <a data-qa="vacancy-serp__vacancy-employer" href="/employer/2"><span data-qa="vacancy-serp__vacancy-employer-text">ООО Лютик</span></a>
  <span data-qa="vacancy-serp__vacancy-address">Санкт-Петербург</span>
</div>
</body></html>"""


# ===========================================================================
# the contract half - no browser needed
# ===========================================================================

def test_the_live_attribute_is_the_one_that_has_to_match():
    """The measured live container comes first, so it is the one that must hit."""
    assert ro.CARD_CONTAINER_SELECTORS[0] == LIVE_CONTAINER


def test_the_legacy_attribute_is_kept_only_as_a_fallback():
    """A board-side rollback must not blind the search again."""
    assert LEGACY_CONTAINER in ro.CARD_CONTAINER_SELECTORS
    assert ro.CARD_CONTAINER_SELECTOR == f"{LIVE_CONTAINER}, {LEGACY_CONTAINER}"


def test_both_scripts_are_built_from_the_one_constant():
    """The bug was a stale *copy*; one constant is what stops a second one."""
    assert ro.CARD_CONTAINER_SELECTOR in ro.COUNT_RESULTS_JS
    assert ro.CARD_CONTAINER_SELECTOR in ro.READ_CARDS_JS


def test_no_substitution_placeholder_survives_into_the_scripts():
    for script in (ro.COUNT_RESULTS_JS, ro.READ_CARDS_JS):
        assert "__CARD_CONTAINER__" not in script
    assert "__SALARY_SELECTOR__" not in ro.READ_CARDS_JS


def test_the_salary_selector_no_longer_accepts_the_frequency_note():
    """``[data-qa*="compensation"]`` matched "Выплаты: ..." - a wrong value.

    Measured live: the only data-qa inside the compensation block belongs to the
    payment-frequency note, so that fallback reported the note as the salary.
    ``None`` is honest; a wrong value is not.
    """
    assert '[data-qa="vacancy-serp__vacancy-compensation"]' in ro.SALARY_SELECTOR
    assert '[class*="compensation-labels"] > span' in ro.SALARY_SELECTOR
    assert '[data-qa*="compensation"]' not in ro.SALARY_SELECTOR


# ===========================================================================
# the DOM half - a real browser, the real structure, and a negative control
# ===========================================================================

@pytest.fixture(scope="module")
def serp_page():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    play = None
    browser = None
    try:
        play = sync_playwright().start()
        browser = play.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - a missing browser is a skip, not a failure
        if play is not None:
            play.stop()
        pytest.skip(f"chromium is not available for the DOM half: {exc}")

    page = browser.new_page()
    page.set_content(SERP_HTML)
    try:
        yield page
    finally:
        browser.close()
        play.stop()


def _evaluate(page, script: str):
    raw = page.evaluate(script)
    return json.loads(raw) if isinstance(raw, str) else raw


def test_the_fixture_reproduces_the_trap(serp_page):
    """The title attribute is alive while the container attribute is gone.

    This is why a ``grep`` for "serp-item" proves nothing: the substring is
    everywhere, the *attribute* is what died.
    """
    counts = serp_page.evaluate(
        "(() => ({"
        "  title_attr: document.querySelectorAll('[data-qa=\"serp-item__title\"]').length,"
        "  container_attr: document.querySelectorAll('[data-qa=\"serp-item\"]').length,"
        "}))()")
    assert counts["title_attr"] == 2, "the fixture must keep the live title attribute"
    assert counts["container_attr"] == 0, "the fixture must not carry the dead container"


def test_count_results_js_counts_the_live_cards(serp_page):
    result = _evaluate(serp_page, ro.COUNT_RESULTS_JS)
    assert result["items"] == 2
    assert result["links"] == 2


def test_read_cards_js_extracts_the_live_cards(serp_page):
    payload = _evaluate(serp_page, ro.READ_CARDS_JS)
    assert payload["count"] == 2
    cards = payload["cards"]
    assert [c["vacancy_id"] for c in cards] == ["111111", "222222"]
    assert cards[0]["title"] == "Инженер технической поддержки"
    assert cards[0]["employer"] == "ООО Ромашка"
    assert cards[1]["area"] == "Санкт-Петербург"


def test_the_salary_is_read_where_there_is_one_and_none_where_there_is_not(serp_page):
    cards = _evaluate(serp_page, ro.READ_CARDS_JS)["cards"]

    with_salary = cards[0]["salary"]
    assert with_salary is not None, "the salary line was not found"
    assert "90 000" in with_salary
    assert "₽" in with_salary
    # and it is the amount, not the payment-frequency note
    assert "Выплаты" not in with_salary

    assert cards[1]["salary"] is None


def test_the_old_selector_finds_nothing_in_the_live_dom(serp_page):
    """The negative control: this is the defect, reproduced.

    If HH ever renders the old attribute again this assertion is what will tell
    us the fallback started carrying weight.
    """
    legacy_only = serp_page.evaluate(
        f"document.querySelectorAll({json.dumps(LEGACY_CONTAINER)}).length")
    assert legacy_only == 0
    live = serp_page.evaluate(
        f"document.querySelectorAll({json.dumps(LIVE_CONTAINER)}).length")
    assert live == 2


def test_the_legacy_reader_reads_zero_cards(serp_page):
    """Put the old container back into the *production* script and it goes blind.

    This is the regression the whole file exists for: it fails on the old
    selector and passes on the new one, on the same document.
    """
    legacy = ro.READ_CARDS_JS.replace(ro.CARD_CONTAINER_SELECTOR, LEGACY_CONTAINER)
    assert legacy != ro.READ_CARDS_JS, (
        "the production script no longer contains the shared container constant, "
        "so this control would be vacuous"
    )
    payload = _evaluate(serp_page, legacy)
    assert payload["count"] == 0
    assert payload["cards"] == []

    fixed = _evaluate(serp_page, ro.READ_CARDS_JS)
    assert fixed["count"] == 2


def test_the_old_salary_fallback_would_have_reported_the_frequency_note(serp_page):
    """The other half of the defect, shown rather than argued.

    ``[data-qa*="compensation"]`` matches the *payment-frequency* span, so the old
    extractor did not return ``None`` on that card - it returned a confident wrong
    value. This control keeps that from creeping back in as a "safe fallback".
    """
    old_salary = '[data-qa="vacancy-serp__vacancy-compensation"], [data-qa*="compensation"]'
    legacy = ro.READ_CARDS_JS.replace(ro.SALARY_SELECTOR, old_salary)
    assert legacy != ro.READ_CARDS_JS, "the shared salary constant is no longer used"

    cards = _evaluate(serp_page, legacy)["cards"]
    # The card that *has* a salary now reports the payment note instead of it.
    assert cards[0]["salary"] is not None
    assert "Выплаты" in cards[0]["salary"]
    assert "90 000" not in cards[0]["salary"]

    fixed = _evaluate(serp_page, ro.READ_CARDS_JS)["cards"]
    assert "90 000" in fixed[0]["salary"]
    assert "Выплаты" not in fixed[0]["salary"]
