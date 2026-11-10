"""T7.27 — the read-selection policy, and the defect it was written for.

The defect: ``run_search_operation`` read ``list(seen)[:read_limit]``, and ``seen``
is in discovery order, which is query order. So ``["IT Support", "Bitrix24"]`` with
``read_limit=10`` read ten IT Support vacancies and **zero** Bitrix24 ones, even
though Bitrix24 had found fifty of its own. The shortlist was decided by the order
of a configuration list.

The tests below hold the old policy next to the new one on the same pool, because a
policy test that only ever sees the new policy proves nothing about the defect.
"""

from __future__ import annotations

import pytest

from ai_assistant import shortlist_selection as sel
from ai_assistant.shortlist_selection import (
    CheapTier,
    ShortlistCategory,
    classify_category,
    cheap_rank,
    select_read_targets,
)


def card(vid: str, title: str, employer: str = "ООО Тест") -> dict:
    return {
        "vacancy_id": vid,
        "title": title,
        "employer": employer,
        "salary": None,
        "area": "Москва",
        "format_label": None,
        "url": f"https://hh.ru/vacancy/{vid}",
    }


def pool(spec: list[tuple[str, str]]) -> dict[str, dict]:
    """A ``seen``-shaped mapping, in discovery order."""
    out: dict[str, dict] = {}
    for vid, title in spec:
        out[vid] = card(vid, title)
    return out


# ===========================================================================
# §1 — the defect itself
# ===========================================================================

def test_the_old_policy_starves_the_second_query():
    """The negative control: this is what the code used to do.

    If this assertion ever stops holding, the regression below is vacuous.
    """
    seen = pool(
        [(f"1000{i}", "Инженер технической поддержки") for i in range(20)]
        + [(f"2000{i}", "Администратор Bitrix24") for i in range(20)]
    )
    old_choice = list(seen)[:10]

    assert all(vid.startswith("1000") for vid in old_choice)
    assert not any(vid.startswith("2000") for vid in old_choice), (
        "the old position-based policy was supposed to starve the second query"
    )


def test_the_balanced_policy_reads_both_queries():
    """§1/§4: a strong direction late in the query list still gets read."""
    seen = pool(
        [(f"1000{i}", "Инженер технической поддержки") for i in range(20)]
        + [(f"2000{i}", "Администратор Bitrix24") for i in range(20)]
    )
    picked = select_read_targets(seen, 10)

    assert len(picked.targets) == 10
    assert len(set(picked.targets)) == 10, "a vacancy must be read at most once"

    by_cat = picked.by_category
    assert len(by_cat.get("IT_SUPPORT", [])) > 0
    assert len(by_cat.get("BITRIX24_CRM", [])) > 0, "Bitrix24 must not be starved"


def test_the_policy_does_not_depend_on_the_order_of_the_pool():
    """The same vacancies in the other order give the same category coverage."""
    forward = pool(
        [(f"1000{i}", "Инженер технической поддержки") for i in range(6)]
        + [(f"2000{i}", "Администратор Bitrix24") for i in range(6)]
        + [(f"3000{i}", "Системный администратор") for i in range(6)]
    )
    backward = pool(list(reversed(list(forward.items()))))

    a = select_read_targets(forward, 9).by_category
    b = select_read_targets(backward, 9).by_category

    assert {k: len(v) for k, v in a.items()} == {k: len(v) for k, v in b.items()}


def test_every_target_direction_gets_a_read_when_it_has_candidates():
    """§4: no direction that has candidates may end at zero."""
    seen = pool(
        [(f"1000{i}", "Инженер технической поддержки") for i in range(10)]
        + [(f"2000{i}", "Администратор Bitrix24") for i in range(10)]
        + [(f"3000{i}", "Системный администратор") for i in range(10)]
        + [(f"4000{i}", "Специалист по сопровождению") for i in range(10)]
        + [(f"5000{i}", "AI Automation Engineer") for i in range(10)]
    )
    picked = select_read_targets(seen, 10)

    for cat in sel.COVERAGE_CATEGORIES:
        assert picked.by_category.get(cat.value), f"{cat.value} was starved"


# ===========================================================================
# §3 — the cheap rank
# ===========================================================================

@pytest.mark.parametrize("title,expected", [
    ("Инженер технической поддержки / Support engineer", ShortlistCategory.IT_SUPPORT),
    ("IT Support Manager", ShortlistCategory.IT_SUPPORT),
    ("Help Desk Engineer", ShortlistCategory.IT_SUPPORT),
    ("Системный администратор", ShortlistCategory.SYSADMIN_INFRA),
    ("Active Directory Administrator", ShortlistCategory.SYSADMIN_INFRA),
    ("IT Infrastructure Engineer", ShortlistCategory.SYSADMIN_INFRA),
    ("Администратор Bitrix24", ShortlistCategory.BITRIX24_CRM),
    ("CRM Support", ShortlistCategory.BITRIX24_CRM),
    ("Специалист по внедрению и сопровождению CRM", ShortlistCategory.BITRIX24_CRM),
    ("Application Support Engineer", ShortlistCategory.APP_INTEGRATION_SUPPORT),
    ("Специалист по сопровождению", ShortlistCategory.APP_INTEGRATION_SUPPORT),
    ("AI Automation Engineer", ShortlistCategory.AUTOMATION_AI),
])
def test_target_directions_are_classified(title, expected):
    assert classify_category(title) is expected


@pytest.mark.parametrize("title", [
    "Руководитель отдела продаж",
    "Менеджер по продажам (системы управления освещением)",
    "AML / Compliance Officer",
    "Head of Localization/Руководитель отдела локализации (Europe)",
    "Куратор клиентского опыта (Customer Experience Specialist)",
    "Менеджер по логистике ВЭД / Таможенный декларант",
    "Senior Full Stack PHP-разработчик",
    "Карьерный консультант / Менеджер по продажам (психология)",
])
def test_off_profile_is_recognised(title):
    """§3: sales / marketing / compliance / localization are not our directions."""
    assert classify_category(title) is ShortlistCategory.OFF_PROFILE


def test_a_sales_role_naming_crm_is_still_off_profile():
    """The trap the live run showed: "Менеджер по продажам CRM" is a sales job."""
    assert classify_category("Менеджер по продажам CRM") is ShortlistCategory.OFF_PROFILE
    assert classify_category("CRM Administrator") is ShortlistCategory.BITRIX24_CRM


def test_the_tiers_follow_the_priority_list():
    assert cheap_rank("Инженер технической поддержки").tier is CheapTier.HIGH
    assert cheap_rank("Администратор Bitrix24").tier is CheapTier.HIGH
    assert cheap_rank("AI Automation Engineer").tier is CheapTier.MEDIUM
    assert cheap_rank("Руководитель отдела продаж").tier is CheapTier.LOW


def test_a_target_support_engineer_is_not_penalised_for_saying_engineer():
    """A penalty on the word "engineer" would have punished the right roles."""
    on_target = cheap_rank("Support Engineer")
    assert on_target.tier is CheapTier.HIGH
    assert on_target.score == cheap_rank("Инженер технической поддержки").score


def test_off_profile_cards_are_not_read_by_default():
    seen = pool([
        ("111", "Руководитель отдела продаж"),
        ("222", "Менеджер по продажам"),
        ("333", "Инженер технической поддержки"),
    ])
    picked = select_read_targets(seen, 10)

    assert picked.targets == ["333"]
    assert picked.off_profile_total == 2


# ===========================================================================
# edges
# ===========================================================================

def test_a_zero_budget_reads_nothing():
    seen = pool([("111", "Инженер технической поддержки")])
    assert select_read_targets(seen, 0).targets == []


def test_a_budget_larger_than_the_pool_returns_the_whole_pool():
    seen = pool([("111", "Инженер технической поддержки"),
                 ("222", "Администратор Bitrix24")])
    picked = select_read_targets(seen, 50)

    assert sorted(picked.targets) == ["111", "222"]
    assert len(set(picked.targets)) == 2


def test_cards_without_an_id_are_skipped():
    seen = {"": card("", "Инженер технической поддержки"),
            "222": card("222", "Системный администратор")}
    assert select_read_targets(seen, 10).targets == ["222"]


def test_the_distribution_helper_counts_per_direction():
    seen = pool([
        ("111", "Инженер технической поддержки"),
        ("222", "Администратор Bitrix24"),
        ("333", "Администратор Bitrix24"),
        ("444", "Руководитель отдела продаж"),
    ])
    dist = sel.category_distribution(seen.keys(), seen)

    assert dist["IT_SUPPORT"] == 1
    assert dist["BITRIX24_CRM"] == 2
    assert dist["OFF_PROFILE"] == 1
    assert dist["AUTOMATION_AI"] == 0


# ===========================================================================
# the bare word "support" is not a direction
# ===========================================================================

@pytest.mark.parametrize("title", [
    "Менеджер по развитию бизнеса (услуга Professional Support)",
    "Customer Support Manager",
    "Менеджер клиентской поддержки",
    "Client Support Manager",
])
def test_client_facing_management_is_not_it_support(title):
    """Measured live: these four were classified IT_SUPPORT/HIGH.

    ``_IT_SUPPORT`` matches the bare token ``support``, so a sales role that
    parenthesises "(Professional Support)" looked like a support engineer and
    reached the TOP of the shortlist. §3 puts sales and client management
    off-profile, and "развитие бизнеса" is sales with a nicer name.
    """
    assert sel.classify_category(title) is ShortlistCategory.OFF_PROFILE
    assert sel.cheap_rank(title).tier is CheapTier.LOW


@pytest.mark.parametrize("title,expected", [
    ("Инженер технической поддержки", ShortlistCategory.IT_SUPPORT),
    ("Support Engineer", ShortlistCategory.IT_SUPPORT),
    ("IT Support Manager", ShortlistCategory.IT_SUPPORT),
    ("Application Support Engineer", ShortlistCategory.APP_INTEGRATION_SUPPORT),
    ("Администратор Bitrix24", ShortlistCategory.BITRIX24_CRM),
    ("Младший системный администратор", ShortlistCategory.SYSADMIN_INFRA),
])
def test_removing_client_management_did_not_break_the_targets(title, expected):
    """The negative control for the fix above.

    Tightening ``_OFF_PROFILE`` is safe only if the roles the profile actually
    targets still classify as themselves. "IT Support Manager" is deliberately
    included: it is *IT* management, not client management, and must survive.
    """
    assert sel.classify_category(title) is expected


# ===========================================================================
# leftovers fill spare capacity, they do not compete for it
# ===========================================================================

def test_leftovers_do_not_take_slots_while_target_cards_remain():
    """Measured live: 269 eligible target cards, budget 30 — and the plain
    round-robin still gave 5 slots to OTHER and 3 to OFF_PROFILE, because those
    buckets took a turn on every lap. A target card left unread while a non-target
    card is read is exactly the waste §4 exists to prevent.
    """
    seen = pool(
        [(f"t{i:03d}", "Инженер технической поддержки") for i in range(20)]
        + [("o001", "Специалист по ветеринарии")]     # OTHER
        + [("x001", "Руководитель отдела продаж")]    # OFF_PROFILE
    )
    picked = select_read_targets(seen, 10, include_off_profile=True)

    assert len(picked.targets) == 10
    assert "o001" not in picked.targets
    assert "x001" not in picked.targets
    assert all(t.startswith("t") for t in picked.targets)


def test_leftovers_are_used_once_the_targets_run_out():
    """The negative control for the rule above: if the targets cannot fill the
    budget, the spare slots must not go to waste."""
    seen = pool(
        [("t001", "Инженер технической поддержки"), ("t002", "Администратор Bitrix24")]
        + [("o001", "Специалист по ветеринарии")]
        + [("x001", "Руководитель отдела продаж")]
    )
    picked = select_read_targets(seen, 4, include_off_profile=True)

    assert len(picked.targets) == 4
    assert {"t001", "t002"} <= set(picked.targets)
    assert "o001" in picked.targets


# ===========================================================================
# leftovers fill spare capacity, they do not compete for it
# ===========================================================================

def test_leftovers_do_not_take_slots_while_target_cards_remain():
    """Measured live: 269 eligible target cards, budget 30 — and the plain
    round-robin still gave 5 slots to OTHER and 3 to OFF_PROFILE, because those
    buckets took a turn on every lap. A target card left unread while a non-target
    card is read is exactly the waste §4 exists to prevent.
    """
    seen = pool(
        [(f"t{i:03d}", "Инженер технической поддержки") for i in range(20)]
        + [("o001", "Специалист по ветеринарии")]     # OTHER
        + [("x001", "Руководитель отдела продаж")]    # OFF_PROFILE
    )
    picked = select_read_targets(seen, 10, include_off_profile=True)

    assert len(picked.targets) == 10
    assert "o001" not in picked.targets
    assert "x001" not in picked.targets
    assert all(t.startswith("t") for t in picked.targets)


def test_leftovers_are_used_once_the_targets_run_out():
    """The negative control for the rule above: if the targets cannot fill the
    budget, the spare slots must not go to waste."""
    seen = pool(
        [("t001", "Инженер технической поддержки"), ("t002", "Администратор Bitrix24")]
        + [("o001", "Специалист по ветеринарии")]
        + [("x001", "Руководитель отдела продаж")]
    )
    picked = select_read_targets(seen, 4, include_off_profile=True)

    assert len(picked.targets) == 4
    assert {"t001", "t002"} <= set(picked.targets)
    assert "o001" in picked.targets
