"""T7.23: read the vacancy's *structure*, not only its prose, before deciding to reply.

WHY THIS MODULE EXISTS
----------------------
T7.22 sent one real response to ``hh:137149827`` and its brief said, in so many words,
"форма занятости не сказана". The rendered page said **«Оформление: Договор ГПХ с ИП»**.
The field was not missing: it sat in ``shortVacancy.civilLawContracts``, inside an object
the brief had already read two other keys from. The conclusion was drawn from prose
(``description``) and from ``acceptLaborContract: false`` - and ``false`` there means
"ТК РФ не предлагают", not "про оформление молчат".

That is the failure this module is built to make impossible: **a field that was never
looked at must never be reported as absent.** Two consequences, both enforced below:

* ``acceptLaborContract`` alone can never produce "оформление не указано" - the reading
  must consult ``civilLawContracts`` as well, and the result records which sources it
  actually read;
* ``workExperience`` alone decides the experience line, and "опыт не указан" is only
  allowed when that key is genuinely empty.

Everything here is pure: it takes the SSR payload and a candidate profile and returns
decisions. No browser, no I/O, no clock - so the whole gate can be tested without a
session, which is the only way a gate gets tested at all.

DECISION VOCABULARY
-------------------
``CLEAR``                  - nothing stands in the way of an automatic reply.
``USER_DECISION_REQUIRED`` - a human must answer first. Never auto-send.
``BLOCKED``                - a definite refusal; the vacancy does not fit.

The middle value is not a soft "BLOCKED": it is the honest answer for a condition that
is real but that only Mikhail can judge (is a ГПХ-with-ИП arrangement workable from
Thailand?). Refusing to guess is the point.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# decision vocabulary
# ---------------------------------------------------------------------------

CLEAR = "CLEAR"
USER_DECISION_REQUIRED = "USER_DECISION_REQUIRED"
BLOCKED = "BLOCKED"

# The three ways a salary question can be unanswerable by arithmetic alone.
SALARY_OK = "SALARY_OK"
SALARY_BELOW_MINIMUM = "SALARY_BELOW_MINIMUM"
SALARY_UNKNOWN = "SALARY_UNKNOWN"
SALARY_NO_REQUIREMENT = "SALARY_NO_REQUIREMENT"
CURRENT_FX_REQUIRED = "CURRENT_FX_REQUIRED"
GROSS_NET_BASIS_UNVERIFIED = "GROSS_NET_BASIS_UNVERIFIED"

# ---------------------------------------------------------------------------
# employment vocabulary
# ---------------------------------------------------------------------------

TK_RF = "TK_RF"
GPH_IP = "GPH_IP"
GPH_SELF_EMPLOYED = "GPH_SELF_EMPLOYED"
GPH_OTHER = "GPH_OTHER"
EMPLOYMENT_UNKNOWN = "UNKNOWN"

# ``civilLawContractsElement`` values seen in HH's SSR payload. The list is
# deliberately a *lookup*, not a filter: an unrecognised value still counts as
# "a form was stated", because the mistake to avoid is calling a stated form absent.
_CIVIL_LAW_FORMS = {
    "INDIVIDUAL_ENTREPRENEUR": (GPH_IP, "Договор ГПХ с ИП"),
    "SELF_EMPLOYED": (GPH_SELF_EMPLOYED, "Договор ГПХ с самозанятым"),
}

EMPLOYMENT_LABELS = {
    TK_RF: "Трудовой договор по ТК РФ",
    GPH_IP: "Договор ГПХ с ИП",
    GPH_SELF_EMPLOYED: "Договор ГПХ с самозанятым",
    GPH_OTHER: "Договор ГПХ (вид не распознан)",
    EMPLOYMENT_UNKNOWN: "не указано",
}

# ``workExperience`` enum -> the words HH prints on the page.
EXPERIENCE_LABELS = {
    "noExperience": "нет опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "более 6 лет",
}

# The structured keys this module insists on seeing. ``fields_missing`` is what a
# caller reports; it is never inferred from a neighbouring key.
REQUIRED_STRUCTURED_FIELDS: tuple[str, ...] = (
    "workExperience",
    "civilLawContracts",
    "acceptLaborContract",
    "workFormats",
    "workSchedule",
    "employment",
    "compensation",
    "area",
    "test",
    "vacancyTests",
    "responseAllowed",
    "responseImpossible",
)


# ---------------------------------------------------------------------------
# the snapshot: what the page actually carries
# ---------------------------------------------------------------------------

STRUCTURED_JS = r"""(() => {
  const vid = '__VID__';
  const out = {vacancy_id: vid, ok: false};
  const tpl = document.querySelector('#HH-Lux-InitialState');
  if (!tpl) { out.error = 'no HH-Lux-InitialState template'; return JSON.stringify(out); }
  let st = null;
  try { st = JSON.parse(tpl.innerHTML || ''); }
  catch (e) { out.error = 'template is not JSON: ' + e; return JSON.stringify(out); }

  const avrs = (st.applicantVacancyResponseStatuses || {})[vid] || {};
  const sv = avrs.shortVacancy || {};
  const vf = (st.vacancyView || {}).vacancyFull || {};
  const v = vf.vacancy || {};

  out.short_vacancy = {
    workExperience: sv.workExperience,
    civilLawContracts: sv.civilLawContracts,
    acceptLaborContract: sv.acceptLaborContract,
    acceptIncompleteResumes: sv.acceptIncompleteResumes,
    workSchedule: sv['@workSchedule'],
    responseLetterRequired: sv['@responseLetterRequired'],
    name: sv.name,
  };
  out.vacancy = {
    compensation: v.compensation,
    area: v.area,
    employment: v.employment,
    employmentForm: v.employmentForm,
    workFormats: v.workFormats,
    workScheduleByDays: v.workScheduleByDays,
    workingHours: v.workingHours,
    experience: v.experience,
    name: v.name,
    id: v.id,
    address: v.address,
    // T7.26 §6: whether the posting is archived. Read from the payload when the
    // payload says; null means "the page did not state it", never "false".
    archived: (typeof v.archived === 'boolean') ? v.archived : null,
  };
  // T7.26 §6: the employer. Read from the vacancy's own company block, because the
  // search result card is not always the page we end up reading.
  out.employer = {
    visibleName: (vf.company || {}).visibleName || (v.company || {}).visibleName || null,
    name: (vf.company || {}).name || (v.company || {}).name || null,
    id: (vf.company || {}).id || (v.company || {}).id || null,
  };
  out.applicant = {
    test: avrs.test,
    vacancyTests: st.vacancyTests,
    startedWithQuestion: st.startedWithQuestion,
    hasQuickResponse: st.hasQuickResponse,
    responseAllowed: avrs.by_country_applicant_visibility
      ? avrs.by_country_applicant_visibility.responseAllowed : null,
    responseImpossible: avrs.responseImpossible,
    alreadyApplied: st.alreadyApplied,
    usedResumeIds: avrs.usedResumeIds,
    hiddenResumeIds: avrs.hiddenResumeIds,
    negotiations_total: (avrs.negotiations || {}).total,
  };
  // T7.26 §6: the payload is the source; the rendered page is the fallback for the
  // two §6 fields the payload may not carry. A fallback that never fires costs one
  // querySelector, and it is never used to *contradict* the payload.
  if (!out.employer.visibleName) {
    const cEl = document.querySelector('[data-qa="vacancy-company-name"]');
    if (cEl) out.employer.visibleName = cEl.innerText.trim();
  }
  if (out.vacancy.archived === null) {
    const aEl = document.querySelector('[data-qa="vacancy-archived"], [data-qa*="vacancy-archived"]');
    if (aEl) out.vacancy.archived = true;
  }
  out.ok = true;
  return JSON.stringify(out);
})()"""


def structured_js(vacancy_id: str | int) -> str:
    """The read for one vacancy. Addressed by vacancy id, like the other readers."""
    return STRUCTURED_JS.replace("__VID__", str(vacancy_id))


def flatten_civil_law_contracts(raw: Any) -> list[str]:
    """``[{"civilLawContractsElement": ["INDIVIDUAL_ENTREPRENEUR"]}]`` -> names.

    HH nests the value one level deep and may send several elements. Anything
    unrecognisable is returned as its ``str`` rather than dropped: a form we cannot
    name is still a form that was stated, and silently dropping it is exactly how
    "не указано" gets written over a real answer.
    """
    names: list[str] = []
    if raw is None:
        return names
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, dict):
            inner = item.get("civilLawContractsElement", item)
        else:
            inner = item
        values = inner if isinstance(inner, list) else [inner]
        for value in values:
            if isinstance(value, dict):
                value = value.get("civilLawContractsElement", value)
            if value is None:
                continue
            names.append(str(value))
    return names


def missing_structured_fields(snapshot: dict[str, Any]) -> list[str]:
    """Which of the required structured keys the page did not carry at all.

    ``None`` counts as missing *for the purposes of this list only* - it means the
    key was absent from the payload. It is never used to conclude that the vacancy
    has no such condition; see ``read_employment``.
    """
    short = snapshot.get("short_vacancy") or {}
    vacancy = snapshot.get("vacancy") or {}
    applicant = snapshot.get("applicant") or {}
    present = {
        "workExperience": short.get("workExperience"),
        "civilLawContracts": short.get("civilLawContracts"),
        "acceptLaborContract": short.get("acceptLaborContract"),
        "workFormats": vacancy.get("workFormats"),
        "workSchedule": short.get("workSchedule"),
        "employment": vacancy.get("employment"),
        "compensation": vacancy.get("compensation"),
        "area": vacancy.get("area"),
        "test": applicant.get("test"),
        "vacancyTests": applicant.get("vacancyTests"),
        "responseAllowed": applicant.get("responseAllowed"),
        "responseImpossible": applicant.get("responseImpossible"),
    }
    return [name for name in REQUIRED_STRUCTURED_FIELDS if present.get(name) is None]


# ---------------------------------------------------------------------------
# employment
# ---------------------------------------------------------------------------

def read_employment(snapshot: dict[str, Any]) -> dict[str, Any]:
    """What form of engagement the employer states, from *all* structured sources.

    The two sources are read together on purpose. ``acceptLaborContract: false`` is
    routinely misread as "no form stated"; in the Dream Team payload it meant the
    opposite - a civil-law contract with an individual entrepreneur was named in
    ``civilLawContracts`` right next to it.
    """
    short = snapshot.get("short_vacancy") or {}
    accept_labor = short.get("acceptLaborContract")
    contracts_raw = short.get("civilLawContracts")
    names = flatten_civil_law_contracts(contracts_raw)

    forms: list[str] = []
    labels: list[str] = []
    if accept_labor is True:
        forms.append(TK_RF)
        labels.append(EMPLOYMENT_LABELS[TK_RF])
    for name in names:
        code, label = _CIVIL_LAW_FORMS.get(
            name.upper(), (GPH_OTHER, f"Договор ГПХ ({name})")
        )
        if code not in forms:
            forms.append(code)
            labels.append(label)

    # Only here - with both sources consulted and both empty - may the answer be
    # "not stated". ``accept_labor is False`` is not that case: it says the labour
    # code is not offered, which is a statement, not a silence.
    stated = bool(forms)
    primary = forms[0] if forms else EMPLOYMENT_UNKNOWN

    return {
        "form": primary,
        "forms": forms,
        "label": EMPLOYMENT_LABELS.get(primary, EMPLOYMENT_LABELS[EMPLOYMENT_UNKNOWN]),
        "labels": labels,
        "stated": stated,
        "accept_labor_contract": accept_labor,
        "civil_law_contracts": names,
        "civil_law_contracts_raw": contracts_raw,
        "sources_read": ["shortVacancy.acceptLaborContract", "shortVacancy.civilLawContracts"],
        "evidence": (
            f"shortVacancy.acceptLaborContract={accept_labor!r}, "
            f"shortVacancy.civilLawContracts={names!r}"
        ),
    }


# ---------------------------------------------------------------------------
# experience
# ---------------------------------------------------------------------------

def read_experience(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The stated experience band, from ``shortVacancy.workExperience``.

    A separate reader because the same T7.22 brief called this "в SSR не заполнено"
    while ``workExperience`` held ``between1And3``. The prose said 6 months-1.5 years;
    the structured field said 1-3. Both are reported, and the structured one is the
    authority for the gate.
    """
    short = snapshot.get("short_vacancy") or {}
    raw = short.get("workExperience")
    vacancy = snapshot.get("vacancy") or {}
    return {
        "work_experience": raw,
        "label": EXPERIENCE_LABELS.get(str(raw), None) if raw else None,
        "stated": raw is not None and str(raw) != "",
        "vacancy_full_experience": vacancy.get("experience"),
        "source": "shortVacancy.workExperience",
    }


# ---------------------------------------------------------------------------
# the employment gate
# ---------------------------------------------------------------------------

def employment_gate(reading: dict[str, Any], thailand_settled: bool = False) -> dict[str, Any]:
    """May we auto-send, given the stated form of engagement?

    Any concrete Russian arrangement (ТК РФ, ГПХ with ИП or a self-employed person)
    is a real condition for someone who lives in Thailand: it decides whether he can
    be engaged at all, and that is not a question arithmetic can answer. So it is
    ``USER_DECISION_REQUIRED`` - not a block, and emphatically not a silent pass.

    ``thailand_settled`` exists for the case where the geography question has already
    been answered by the employer in writing; until that happens it is False.
    """
    if not reading.get("stated"):
        return {
            "decision": USER_DECISION_REQUIRED,
            "code": "EMPLOYMENT_UNKNOWN",
            "reason": (
                "ни acceptLaborContract, ни civilLawContracts не называют форму "
                "оформления — неизвестно, на каких условиях предлагается работа"
            ),
            "form": EMPLOYMENT_UNKNOWN,
            "label": EMPLOYMENT_LABELS[EMPLOYMENT_UNKNOWN],
        }

    if thailand_settled:
        return {
            "decision": CLEAR,
            "code": "EMPLOYMENT_SETTLED",
            "reason": "форма оформления известна и вопрос о работе из Таиланда уже закрыт",
            "form": reading["form"],
            "label": reading["label"],
        }

    return {
        "decision": USER_DECISION_REQUIRED,
        "code": "EMPLOYMENT_NEEDS_THAILAND_ANSWER",
        "reason": (
            f"вакансия называет форму оформления «{reading['label']}», а работа из "
            "Таиланда при такой форме не подтверждена — решает Михаил"
        ),
        "form": reading["form"],
        "label": reading["label"],
    }


# ---------------------------------------------------------------------------
# the salary gate
# ---------------------------------------------------------------------------

def _norm_currency(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    # HH writes RUR; the profile writes USD. Both are the ISO-ish codes in play.
    return {"RUR": "RUB", "РУБ": "RUB"}.get(text, text)


def salary_gate(snapshot: dict[str, Any], profile: Any) -> dict[str, Any]:
    """Compare the vacancy's pay with the candidate's floor - or refuse to.

    Three refusals, each with its own honest name:

    * ``SALARY_UNKNOWN`` - the vacancy states no figure. Unknown is *not* "below";
      the project has read an absent figure as a fact before (T7.3), and this gate
      does not repeat it.
    * ``CURRENT_FX_REQUIRED`` - the figures are in different currencies. Converting
      would make the answer depend on today's rate, so the gate declines to guess.
    * ``GROSS_NET_BASIS_UNVERIFIED`` - the vacancy's figure is gross and the floor's
      basis is not declared, so "above the floor" cannot be asserted: a gross figure
      can clear a floor that its net value would miss. Gross and net are not mixed.

    And one refusal that is not a refusal at all: no configured minimum means there
    is no question to answer.
    """
    vacancy = snapshot.get("vacancy") or {}
    comp = vacancy.get("compensation") or {}

    floor = getattr(profile, "minimum_salary", None)
    floor_currency = _norm_currency(getattr(profile, "salary_currency", None))
    floor_gross = getattr(profile, "salary_gross", None)  # absent today: None

    if floor is None:
        return {
            "decision": CLEAR,
            "code": "SALARY_NO_REQUIREMENT",
            "reason": "в профиле не задан минимум дохода — сравнивать не с чем",
            "vacancy_amount": None,
            "vacancy_currency": None,
            "vacancy_gross": comp.get("gross"),
            "floor": None,
            "floor_currency": floor_currency,
            "floor_gross": floor_gross,
        }

    amount = comp.get("to") if comp.get("to") is not None else comp.get("from")
    currency = _norm_currency(comp.get("currencyCode"))
    gross = comp.get("gross")
    base = {
        "vacancy_amount": amount,
        "vacancy_currency": currency,
        "vacancy_gross": gross,
        "floor": floor,
        "floor_currency": floor_currency,
        "floor_gross": floor_gross,
    }

    if amount is None:
        return {
            "decision": CLEAR,  # unknown does not block; it is recorded, not read as below
            "code": SALARY_UNKNOWN,
            "reason": "зарплата в вакансии не указана — это не «ниже минимума», это неизвестно",
            **base,
        }

    if floor_currency and currency and floor_currency != currency:
        return {
            "decision": USER_DECISION_REQUIRED,
            "code": CURRENT_FX_REQUIRED,
            "reason": (
                f"вакансия платит {amount} {currency}, минимум задан в "
                f"{floor} {floor_currency} — сравнение требует курса на сегодня, "
                "а курс не угадывается"
            ),
            **base,
        }

    if amount < floor:
        return {
            "decision": BLOCKED,
            "code": SALARY_BELOW_MINIMUM,
            "reason": (
                f"{amount} {currency or ''} ниже установленного минимума "
                f"{floor} {floor_currency or ''}".strip()
            ),
            **base,
        }

    # Above the floor in the same currency - but only comparable if the bases agree.
    # A gross vacancy and a floor of undeclared basis can disagree after tax, so the
    # comparison is reported, not trusted.
    if gross is True and floor_gross is None:
        return {
            "decision": USER_DECISION_REQUIRED,
            "code": GROSS_NET_BASIS_UNVERIFIED,
            "reason": (
                f"{amount} {currency or ''} указаны gross, а базис минимума "
                f"({floor} {floor_currency or ''}) не объявлен — gross и net не смешиваются"
            ),
            **base,
        }

    if gross is False and floor_gross is True:
        return {
            "decision": USER_DECISION_REQUIRED,
            "code": GROSS_NET_BASIS_UNVERIFIED,
            "reason": "вакансия называет сумму net, а минимум задан gross — базы разные",
            **base,
        }

    return {
        "decision": CLEAR,
        "code": SALARY_OK,
        "reason": (
            f"{amount} {currency or ''} не ниже минимума {floor} {floor_currency or ''}"
        ).strip(),
        **base,
    }


# ---------------------------------------------------------------------------
# the combined preflight
# ---------------------------------------------------------------------------

def structured_preflight(snapshot: dict[str, Any], profile: Any,
                         thailand_settled: bool = False) -> dict[str, Any]:
    """The whole structured decision: employment + experience + salary + read coverage.

    Returns a verdict plus the two lists a caller acts on. ``blockers`` means "do not
    send, this vacancy does not fit"; ``user_decision_required`` means "do not send,
    a human must answer first". The two are kept apart because they need different
    words in a report and different handling in a queue.

    ``thailand_settled`` is False until an employer has answered the geography
    question in writing; until then every stated form of engagement is an open
    question rather than a silent pass.
    """
    missing = missing_structured_fields(snapshot)
    employment = read_employment(snapshot)
    experience = read_experience(snapshot)

    gates = {
        "employment": employment_gate(employment, thailand_settled=thailand_settled),
        "salary": salary_gate(snapshot, profile),
    }

    blockers: list[str] = []
    decisions: list[str] = []
    for name, gate in gates.items():
        if gate["decision"] == BLOCKED:
            blockers.append(f"{name}: {gate['code']} — {gate['reason']}")
        elif gate["decision"] == USER_DECISION_REQUIRED:
            decisions.append(f"{name}: {gate['code']} — {gate['reason']}")

    if blockers:
        verdict = BLOCKED
    elif decisions:
        verdict = USER_DECISION_REQUIRED
    else:
        verdict = CLEAR

    return {
        "verdict": verdict,
        "blockers": blockers,
        "user_decision_required": decisions,
        "auto_send_permitted": verdict == CLEAR,
        "employment": employment,
        "experience": experience,
        "gates": gates,
        "fields_missing": missing,
        "fields_read": [
            name for name in REQUIRED_STRUCTURED_FIELDS if name not in missing
        ],
        "read_complete": not missing,
        # T7.26 §6: the two §6 material fields the snapshot now carries. Reported, not
        # gated - they describe the posting, they do not decide whether we may send.
        # ``archived`` is None when the page did not state it, never False by default.
        "employer": snapshot.get("employer") or {},
        "archived": (snapshot.get("vacancy") or {}).get("archived"),
    }


def human_summary(preflight: dict[str, Any]) -> str:
    """The one-line-per-fact summary a report or a chat message can quote."""
    employment = preflight["employment"]
    experience = preflight["experience"]
    lines = [
        f"Оформление: {employment['label']}",
        f"Опыт: {experience['label'] or 'не указан'}"
        + (f" (workExperience={experience['work_experience']})"
           if experience["work_experience"] else ""),
        f"Вердикт: {preflight['verdict']}",
    ]
    for item in preflight["blockers"]:
        lines.append(f"  БЛОК: {item}")
    for item in preflight["user_decision_required"]:
        lines.append(f"  РЕШЕНИЕ МИХАИЛА: {item}")
    if preflight["fields_missing"]:
        lines.append(f"  Не прочитано из структуры: {', '.join(preflight['fields_missing'])}")
    return "\n".join(lines)
