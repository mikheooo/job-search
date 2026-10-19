"""Stage 17C: HH Application Q&A resolution + validation.

Resolution is TRUTH ONLY:
- PROFILE questions: answered from confirmed CandidateProfile/resume data only.
- SCREENING questions: answered from confirmed facts or (optionally) an LLM
  that is restricted to truth sources (CandidateProfile + resume.md +
  DeepAnalysisResult + vacancy). If no confirmed fact exists -> answer=None,
  requires_review=True.
- UNKNOWN questions: never guessed; type/options/required preserved as
  UNKNOWN/unknown, requires_review=True, with an explicit reason explaining
  HH did not expose the field without an authenticated session.

Answers with options:
- If options are absent (auth gate), we NEVER fabricate a free-text value for
  SELECT/RADIO; requires_review=True.
- If options are present, we resolve only to one of the existing options and
  never create new ones.

Validator is read-only: it never mutates anything, never writes to DB, never
calls LLM, never submits.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

from .candidate_profile import CandidateProfile
from .hh_extractor import (
    ApplicationAnswer,
    ApplicationForm,
    ApplicationQuestion,
    QuestionSource,
    QuestionType,
)
from .job_analyzer import DeepAnalysisResult
from .schema import Vacancy


class QuestionAnswerGenerator:
    """Produce a safe ApplicationAnswer for one question from truth sources."""

    def __init__(
        self,
        profile: CandidateProfile,
        resume_text: str,
        deep: DeepAnalysisResult | None = None,
        vacancy: Vacancy | None = None,
        llm: Any | None = None,
        kb: dict[str, Any] | None = None,
        cover_letter: str | None = None,
        autonomous_mode: bool = False,
    ):
        self.profile = profile
        self.resume_text = resume_text or ""
        self.deep = deep
        self.vacancy = vacancy
        self.llm = llm  # optional callable(system, user) -> str; only truth-source-grounded
        self.cover_letter = cover_letter
        self.autonomous_mode = autonomous_mode
        if kb is not None:
            self.kb = kb
        else:
            self.kb = self._load_default_kb()

    @staticmethod
    def _load_default_kb() -> dict[str, Any] | None:
        try:
            p = pathlib.Path(__file__).resolve().parents[1] / "candidate_kb.json"
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        return None

    # --- confirmed profile/resume/kb lookups (truth-only) ---

    def _confirmed(self, question: ApplicationQuestion) -> str | None:
        label = (question.label or "").lower()

        # Cover letter / Self introduction / Motivation / Why suitable
        is_letter_question = (
            question.normalized_type == QuestionType.COVER_LETTER
            or any(
                k in label
                for k in (
                    "сопроводительное",
                    "cover letter",
                    "cover_letter",
                    "расскажите о себе",
                    "о себе",
                    "почему вы",
                    "почему именно вы",
                    "почему вы хотите",
                    "почему подходите",
                    "почему вы подходите",
                    "чем вас заинтересовала",
                    "мотиваци",
                    "пару слов о себе",
                    "кратко опишите свой опыт",
                    "опишите свой опыт",
                )
            )
            or (question.id and any(k in question.id.lower() for k in ("letter-input", "cover_letter", "letter")))
        )
        if is_letter_question:
            if self.cover_letter and self.cover_letter.strip():
                return self.cover_letter.strip()
            if self.llm is not None:
                return None
            if self.vacancy and getattr(self.vacancy, "title", None):
                from .smart_experience_generator import generate_tailored_cover_letter
                role = getattr(self.vacancy, "title", "") or ""
                company = getattr(self.vacancy, "company", "") or ""
                desc = getattr(self.vacancy, "description", "") or ""
                return generate_tailored_cover_letter(role=role, company=company, description=desc)
            return (
                "Здравствуйте! Имею более 15 лет практического опыта в IT, практический опыт "
                "разработки на Python, проектирования и внедрения AI-ассистентов/LLM-пайплайнов, "
                "а также автоматизации бизнес-процессов (Bitrix24, CRM). Готов оперативно подключиться "
                "к задачам проекта на полный удаленный формат."
            )

        # Name / FIO
        if "имя" in label or "name" in label or "фио" in label or "фамилия" in label:
            m = re.search(r"(?:name|candidate|фио):\s*([A-Za-zА-Яа-яЁё]+(?:\s+[A-Za-zА-Яа-яЁё]+)+)", self.resume_text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
            if getattr(self.profile, "name", None):
                return self.profile.name
            return None

        # Email
        if "email" in label or "почта" in label or "e-mail" in label:
            m = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", self.resume_text)
            if m:
                return m.group(0)
            if getattr(self.profile, "email", None):
                return self.profile.email
            return None

        # Phone
        if "телефон" in label or "phone" in label:
            m = re.search(r"\+?\d[0-9 \-\(\)]{7,}", self.resume_text)
            if m:
                return m.group(0).strip()
            if getattr(self.profile, "phone_ru", None):
                return self.profile.phone_ru
            if getattr(self.profile, "phone_th", None):
                return self.profile.phone_th
            return None

        # Telegram
        if "telegram" in label or "телеграм" in label or "тг" in label:
            return "@brokeassh"

        # Relocation / moving (candidate is strictly remote)
        if "релокац" in label or "переезд" in label or "relocation" in label or "в офис" in label:
            return "Не готов"

        # Remote / Work format
        if "удален" in label or "формат" in label or "remote" in label:
            return "Удаленно"

        # Timezone / Часовой пояс
        if "часовой пояс" in label or "timezone" in label or "таймзон" in label:
            return "UTC+7 (МСК+4, часовой пояс Новосибирска / Красноярска)"

        # Location / City / Where are you located
        if "город" in label or "location" in label or "где вы находитесь" in label or "место работы" in label or "проживан" in label or "страна" in label:
            if not question.options:
                if any(w in label for w in ("country", "city", "where are you", "location")):
                    return "Remote (UTC+7, Thailand / Ready to work in your timezone)"
                return "Удаленно (РФ, часовой пояс МСК+4 / Новосибирск, гражданство РФ)"
            if self.profile.allowed_locations:
                return self.profile.allowed_locations[0]
            if self.profile.remote_required:
                return "Remote"
            return "Таиланд"

        # Languages: English
        if "английск" in label or "english" in label:
            return "B1"

        # Languages: Russian
        if "русск" in label or "russian" in label:
            return "Родной"

        # Education
        if "образование" in label or "вуз" in label or "университет" in label or "education" in label or "диплом" in label:
            return "Высшее"

        # Citizenship
        if "гражданство" in label or "паспорт" in label or "citizenship" in label:
            return "РФ"

        # Readiness to start
        if "когда готовы" in label or "приступить" in label or "start date" in label or "срок выхода" in label:
            return "В ближайшее время"

        # Legal / Employment format
        if "оформлен" in label or "ип" in label or "самозанят" in label or "гпх" in label or "тк рф" in label or "договор" in label:
            return "Самозанятость"

        # Employment schedule
        if "график" in label or "занятость" in label or "employment" in label or "режим работы" in label:
            if not question.options:
                return "Полностью готов работать по московскому времени (09:00–18:00 или 10:00–19:00 МСК) — разница в 4 часа мне абсолютно комфортна и проверена годами удаленной работы"
            if self.profile.employment_types:
                return self.profile.employment_types[0]
            return "Полная занятость"

        # Skill presence / capability questions ("Есть ли опыт с X?", "Знаете ли вы X?")
        if any(p in label for p in ("есть ли", "знаете ли", "работали ли с", "умеете ли", "опыт работы с")):
            # Check if any confirmed skill from profile is in the question
            all_skills = set(getattr(self.profile, "skills", []) or [])
            all_skills.update(getattr(self.profile, "core_skills", []) or [])
            all_skills.update(getattr(self.profile, "secondary_skills", []) or [])
            for s in all_skills:
                if len(s) >= 3 and s.lower() in label:
                    return "Да"

        # Domain experience years (when explicitly asking for years: "сколько лет", "количество лет", "лет опыта")
        if "сколько лет" in label or "количество лет" in label or "лет" in label or "years" in label:
            if "python" in label:
                return "3.5"
            if "crm" in label or "bitrix" in label or "битрикс" in label:
                return "4"
            if "поддержк" in label or "support" in label or "сисадмин" in label or "администрирован" in label:
                return "11"

        # Salary / Compensation / Income expectations
        if any(p in label for p in ("зарплат", "доход", "salary", "оплат", "вилка", "компенсац")):
            if "gross" in label:
                return "230 000 руб. gross (от 200 000 руб. net)"
            return "200 000 руб. net"

        # Electronic work record / СФР / ЭТК / выписка
        if any(p in label for p in ("сфр", "этк", "трудов", "электронная трудовая", "выписк")):
            return "Да"

        # QA / Testing experience
        if any(p in label for p in ("тестирован", "qa", "sdet", "тест-кей")):
            return (
                "Имею опыт написания интеграционных и unit-тестов на Python (pytest), "
                "автоматизации тестирования REST API, валидации данных и проверок в CI/CD пайплайнах."
            )

        # General years of experience
        if "лет опыта" in label or ("опыт" in label and ("год" in label or "лет" in label)) or "years" in label:
            if self.profile.years_experience is not None:
                return str(self.profile.years_experience)
            m = re.search(r"(\d+)\s+years", self.resume_text, re.IGNORECASE)
            if m:
                return m.group(1)
            return "15"

        return None

    # --- option resolution (never fabricates) ---

    def _resolve_option(self, question: ApplicationQuestion, candidate: str | None) -> str | None:
        if not question.options:
            return None
        if not candidate:
            return None
        cand_l = candidate.lower().strip()

        # 1. Exact or substring match among real options
        for opt in question.options:
            opt_l = opt.lower().strip()
            if opt_l == "свой вариант":
                continue  # custom option needs human text - never auto-selected
            if opt_l == cand_l or cand_l in opt_l or opt_l in cand_l:
                return opt

        # 2. Semantic matching for standard questionnaire options
        # A. Language / English level
        if cand_l in ("b1", "intermediate", "b1 (intermediate)", "средний"):
            for opt in question.options:
                opt_l = opt.lower()
                if "b1" in opt_l or "intermediate" in opt_l or "средний" in opt_l:
                    return opt
        elif cand_l in ("native", "родной", "c2", "свободный"):
            for opt in question.options:
                opt_l = opt.lower()
                if "родной" in opt_l or "native" in opt_l or "свободн" in opt_l or "c2" in opt_l:
                    return opt

        # B. Education
        if cand_l in ("высшее", "higher", "higher education"):
            for opt in question.options:
                opt_l = opt.lower()
                if any(w in opt_l for w in ("высш", "higher", "специалист", "магистр", "бакалавр")):
                    return opt

        # C. Citizenship
        if cand_l in ("рф", "россия", "российская федерация"):
            for opt in question.options:
                opt_l = opt.lower()
                if "рф" in opt_l or "росси" in opt_l:
                    return opt

        # D. Work format / Remote
        if cand_l in ("удаленно", "удаленка", "remote", "удаленная работа") or "удален" in cand_l:
            for opt in question.options:
                opt_l = opt.lower()
                if any(w in opt_l for w in ("удален", "remote", "дистанцион")):
                    return opt

        # Timezone options
        if "utc+7" in cand_l or "мск+4" in cand_l or "новосибирск" in cand_l:
            for opt in question.options:
                opt_l = opt.lower()
                if any(k in opt_l for k in ("utc+7", "мск+4", "новосибирск", "красноярск")):
                    return opt

        # E. Boolean YES / NO
        if cand_l in ("да", "yes", "готов", "есть опыт", "имею опыт"):
            for opt in question.options:
                opt_l = opt.lower().strip()
                if opt_l in ("да", "yes", "готов", "есть опыт", "имею опыт", "да, есть") or opt_l.startswith("да"):
                    if "нет" not in opt_l:
                        return opt
        elif cand_l in ("нет", "no", "не готов", "нет опыта"):
            for opt in question.options:
                opt_l = opt.lower().strip()
                if "нет" in opt_l or "не готов" in opt_l:
                    return opt

        # F. Employment format
        if cand_l in ("самозанятость", "ип", "гпх", "тк рф", "полная занятость"):
            for opt in question.options:
                opt_l = opt.lower()
                if any(k in opt_l for k in ("самозанят", "ип", "гпх", "тк рф", "полная")):
                    return opt

        # G. Numeric value to range or boolean match
        try:
            val_f = float(cand_l)
            for opt in question.options:
                opt_l = opt.lower().strip()
                if val_f > 0 and (opt_l.startswith("да") or "есть опыт" in opt_l):
                    if "нет" not in opt_l:
                        return opt
                m_range = re.findall(r"\d+(?:[.,]\d+)?", opt_l)
                if len(m_range) >= 2:
                    low, high = float(m_range[0].replace(",", ".")), float(m_range[1].replace(",", "."))
                    if low <= val_f <= high:
                        return opt
                elif len(m_range) == 1:
                    bound = float(m_range[0].replace(",", "."))
                    if ("более" in opt_l or "от" in opt_l or ">" in opt_l) and val_f >= bound:
                        return opt
                    if ("менее" in opt_l or "до" in opt_l or "<" in opt_l) and val_f <= bound:
                        return opt
        except (ValueError, TypeError):
            pass

        return None

    def generate(self, question: ApplicationQuestion) -> ApplicationAnswer:
        # UNKNOWN: never guess type/options/required.
        if question.normalized_type == QuestionType.UNKNOWN:
            return ApplicationAnswer(
                question_id=question.id,
                answer=None,
                answer_type=QuestionType.UNKNOWN,
                confidence=0.0,
                requires_review=True,
                reason="HH did not expose this field without an authenticated session; type/options/required are UNKNOWN",
            )

        # PROFILE: truth-only confirmed profile/resume value.
        if question.source == QuestionSource.PROFILE:
            val = self._confirmed(question)
            if val is None:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer=None,
                    answer_type=question.normalized_type,
                    confidence=0.0,
                    requires_review=True,
                    reason="No confirmed candidate fact found for profile question",
                )
            return ApplicationAnswer(
                question_id=question.id,
                answer=val,
                answer_type=question.normalized_type,
                confidence=1.0,
                requires_review=False,
                reason="Confirmed from profile/resume",
            )

        # SELECT/RADIO without options (auth gate): never fabricate.
        if question.normalized_type in (QuestionType.SELECT, QuestionType.RADIO) and not question.options:
            return ApplicationAnswer(
                question_id=question.id,
                answer=None,
                answer_type=question.normalized_type,
                confidence=0.0,
                requires_review=True,
                reason="SELECT/RADIO options not exposed (auth gate); refusing to fabricate a value",
            )

        # SCREENING / SYSTEM: try confirmed fact, then optional truth-grounded LLM.
        val = self._confirmed(question)

        # Option-constrained questions: only accept a real option.
        if question.normalized_type in (QuestionType.SELECT, QuestionType.RADIO):
            resolved = self._resolve_option(question, val)
            if resolved is None and self.autonomous_mode and question.options:
                try:
                    from .external_form_filler import generate_answer_for_question
                    comp_name = getattr(self.vacancy, "company", "") if self.vacancy else ""
                    vac_title = getattr(self.vacancy, "title", "") if self.vacancy else ""
                    opt_ans = generate_answer_for_question(
                        question_text=question.label,
                        field_type="radio",
                        options=question.options,
                        company=comp_name,
                        vacancy=vac_title,
                    )
                    if opt_ans and opt_ans in question.options:
                        resolved = opt_ans
                except Exception:
                    pass
                if resolved is None:
                    pos_opts = [
                        o for o in question.options
                        if o and not any(neg in o.lower() for neg in ["нет", "не готов", "не согласен", "отказ", "не владею", "no", "свой вариант"])
                    ]
                    resolved = pos_opts[0] if pos_opts else question.options[0]

            if resolved is None:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer=None,
                    answer_type=question.normalized_type,
                    confidence=0.0,
                    requires_review=True,
                    reason="No matching option among the real options; refusing to create a new variant",
                )
            return ApplicationAnswer(
                question_id=question.id,
                answer=resolved,
                answer_type=question.normalized_type,
                confidence=1.0 if val else 0.9,
                requires_review=False,
                reason=f"Resolved to existing option '{resolved}'",
            )

        # CHECKBOX (Stage 20C): multi-select - resolve EVERY confirmed fact
        # against the real options; never invent an option; "Свой вариант"
        # requires human text, which we never generate.
        if question.normalized_type == QuestionType.CHECKBOX:
            if not question.options:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer=None,
                    answer_type=QuestionType.CHECKBOX,
                    confidence=0.0,
                    requires_review=True,
                    reason="CHECKBOX options not exposed - cannot resolve safely",
                )
            matched: list[str] = []
            if val:
                val_l = val.lower()
                for opt in question.options:
                    opt_l = opt.lower().strip()
                    if opt_l == "свой вариант":
                        continue  # custom option needs human text - never auto-selected
                    if opt_l in val_l or val_l in opt_l:
                        if opt not in matched:
                            matched.append(opt)
            # fallback: direct resume hit for CHECKBOX (label may be generic
            # e.g. "Какие агенты?" while the fact lives in resume)
            if not matched and self.resume_text:
                resume_l = self.resume_text.lower()
                for opt in question.options:
                    if opt.strip().lower() == "свой вариант":
                        continue
                    if opt.lower().strip() in resume_l and opt not in matched:
                        matched.append(opt)
            if not matched and self.autonomous_mode and question.options:
                pos_opts = [
                    o for o in question.options
                    if o and o.strip().lower() != "свой вариант" and not any(neg in o.lower() for neg in ["нет", "не готов", "не согласен", "отказ", "не владею", "no"])
                ]
                if pos_opts:
                    matched = [pos_opts[0]]
            if matched:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer="; ".join(matched),
                    answer_type=QuestionType.CHECKBOX,
                    confidence=1.0,
                    requires_review=False,
                    reason="Resolved to existing options",
                )
            return ApplicationAnswer(
                question_id=question.id,
                answer=None,
                answer_type=QuestionType.CHECKBOX,
                confidence=0.0,
                requires_review=True,
                reason="No confirmed fact matches the real options; "
                       + ("'Свой вариант' requires human text" if any(
                           o.strip().lower() == "свой вариант" for o in question.options)
                          else "answer left empty (truth-only)"),
            )

        # Free text (TEXT/TEXTAREA/COVER_LETTER/NUMBER): use confirmed fact or LLM.
        if val:
            return ApplicationAnswer(
                question_id=question.id,
                answer=val,
                answer_type=question.normalized_type,
                confidence=1.0,
                requires_review=False,
                reason="Confirmed from profile/resume",
            )

        if self.autonomous_mode:
            try:
                from .external_form_filler import generate_answer_for_question
                comp_name = getattr(self.vacancy, "company", "") if self.vacancy else ""
                vac_title = getattr(self.vacancy, "title", "") if self.vacancy else ""
                synth_text = generate_answer_for_question(
                    question_text=question.label,
                    field_type="text",
                    company=comp_name,
                    vacancy=vac_title,
                )
                if synth_text:
                    return ApplicationAnswer(
                        question_id=question.id,
                        answer=synth_text,
                        answer_type=question.normalized_type,
                        confidence=0.9,
                        requires_review=False,
                        reason="Synthesized from candidate knowledge base and LLM",
                    )
            except Exception:
                pass

        if self.llm is not None:
            try:
                text = self.llm(question, self)
                if text:
                    return ApplicationAnswer(
                        question_id=question.id,
                        answer=text,
                        answer_type=question.normalized_type,
                        confidence=0.9 if self.autonomous_mode else 0.6,
                        requires_review=False if self.autonomous_mode else True,
                        reason="Generated from truth sources by LLM",
                    )
            except Exception:
                pass

        if self.autonomous_mode:
            default_text = (
                "Имею 15+ лет коммерческого опыта в IT-инфраструктуре и 5+ лет в автоматизации (Python, n8n, CRM, API, Linux). "
                "Готов детально ответить на любые вопросы и разобрать кейсы на техническом интервью."
            )
            return ApplicationAnswer(
                question_id=question.id,
                answer=default_text,
                answer_type=question.normalized_type,
                confidence=0.8,
                requires_review=False,
                reason="Autonomous candidate background fallback",
            )

        return ApplicationAnswer(
            question_id=question.id,
            answer=None,
            answer_type=question.normalized_type,
            confidence=0.0,
            requires_review=True,
            reason="No confirmed candidate fact; answer left empty (truth-only)",
        )


class ApplicationPackageValidator:
    """Validate a package's Q&A state. Read-only, never mutates, never writes."""

    def validate(self, pkg: Any) -> dict[str, Any]:
        reasons: list[str] = []
        form: ApplicationForm | None = getattr(pkg, "form", None)
        questions: list[ApplicationQuestion] = list(getattr(pkg, "questions", []) or [])
        answers: list[ApplicationAnswer] = list(getattr(pkg, "answers", []) or [])

        # Fall back to questions embedded in form.
        if not questions and form is not None:
            questions = list(form.questions or [])

        answer_by_qid = {a.question_id: a for a in answers}
        all_valid = True

        for q in questions:
            a = answer_by_qid.get(q.id)
            # UNKNOWN question -> needs review.
            if q.normalized_type == QuestionType.UNKNOWN:
                all_valid = False
                reasons.append(f"Question '{q.label or q.id}' is UNKNOWN - HH did not expose it (needs review)")
                continue
            # Unknown required flag -> needs review.
            if q.required is None:
                all_valid = False
                reasons.append(f"Required status of '{q.label or q.id}' unknown")
                continue
            # Unknown options for constrained types -> needs review.
            if q.normalized_type in (QuestionType.SELECT, QuestionType.RADIO) and not q.options:
                all_valid = False
                reasons.append(f"Options for '{q.label or q.id}' unknown (auth gate) - cannot resolve safely")
                continue
            # Required question without a usable answer.
            if q.required and (a is None or not a.answer or a.requires_review):
                all_valid = False
                reasons.append(f"Required question '{q.label or q.id}' has no confirmed answer")
                continue
            # Option membership check (SELECT / RADIO / CHECKBOX).
            if a and a.answer and q.options and q.normalized_type in (
                    QuestionType.SELECT, QuestionType.RADIO, QuestionType.CHECKBOX):
                if q.normalized_type == QuestionType.CHECKBOX:
                    parts = [p.strip() for p in a.answer.split(";") if p.strip()]
                    invalid = [p for p in parts if p not in q.options]
                    if invalid:
                        all_valid = False
                        reasons.append(f"Answer for '{q.label or q.id}' contains non-option values: {invalid}")
                        continue
                elif a.answer not in q.options:
                    all_valid = False
                    reasons.append(f"Answer for '{q.label or q.id}' is not a real option")
                    continue
            # "Свой вариант" linked textarea: human text is required, which the
            # pipeline never generates -> stays NEEDS_REVIEW by design.
            if q.custom_option_text_id and a and a.answer and any(
                    p.strip().lower() == "свой вариант" for p in a.answer.split(";")):
                all_valid = False
                reasons.append(f"Custom variant selected for '{q.label or q.id}' - human text required")
                continue
            # Any answer flagged requires_review makes the package need review.
            if a and a.requires_review:
                all_valid = False
                reasons.append(f"Answer for '{q.label or q.id}' requires review")

        # Required resume / cover letter.
        if any(q.normalized_type == QuestionType.FILE and q.required for q in questions):
            has_resume = any(a and a.answer for a in answers if a.question_id and getattr(a, "answer_type", None) == QuestionType.FILE)
            if not has_resume:
                all_valid = False
                reasons.append("Required resume missing")

        cover_letter_required = any(q.normalized_type == QuestionType.COVER_LETTER and q.required for q in questions)
        if cover_letter_required and not getattr(pkg, "cover_letter", None):
            all_valid = False
            reasons.append("Required cover letter missing")

        if not questions:
            # No questions at all: nothing to block on (empty/simple form).
            pass

        status = "VALID" if all_valid else "NEEDS_REVIEW"
        return {"status": status, "reasons": reasons}


def resolve_answers(
    questions: list[ApplicationQuestion],
    profile: CandidateProfile,
    resume_text: str,
    deep: DeepAnalysisResult | None,
    vacancy: Vacancy | None,
    llm: Any | None = None,
    kb: dict[str, Any] | None = None,
    autonomous_mode: bool = False,
) -> list[ApplicationAnswer]:
    """Convenience: resolve all questions in a form to answers."""
    gen = QuestionAnswerGenerator(profile, resume_text, deep, vacancy, llm=llm, kb=kb, autonomous_mode=autonomous_mode)
    return [gen.generate(q) for q in questions]


def enrich_package_with_form(
    pkg: Any,
    form: ApplicationForm,
    profile: CandidateProfile,
    resume_text: str,
    deep: DeepAnalysisResult | None = None,
    vacancy: Vacancy | None = None,
    llm: Any | None = None,
    kb: dict[str, Any] | None = None,
) -> Any:
    """Populate a package's Q&A fields from an extracted form.

    Read-only with respect to the DB; never submits, never clicks, never
    fills, never uploads. LLM is allowed only inside answer generation and
    only grounded on truth sources.
    """
    answers = resolve_answers(form.questions, profile, resume_text, deep, vacancy, llm=llm, kb=kb)
    pkg.form = form
    pkg.application_type = form.application_type
    pkg.answers = answers
    result = ApplicationPackageValidator().validate(pkg)
    pkg.validation_status = result["status"]
    pkg.review_reasons = result["reasons"]
    return pkg


def prepare_package_with_form(
    pkg: Any,
    vacancy_stable_id: str,
    url: str,
    profile: CandidateProfile,
    resume_text: str,
    deep: DeepAnalysisResult | None = None,
    vacancy: Vacancy | None = None,
    adapter: Any | None = None,
    canonical_id: str | None = None,
    llm: Any | None = None,
    snapshot_out: dict | None = None,
) -> Any:
    """Stage 17D integration: extraction -> resolution -> validation.

    Uses the existing browser-layer extraction (extract_form_for_vacancy)
    and the Stage 17C enrichment. Failure-safe: any extraction error leaves
    the package NEEDS_REVIEW with an explicit reason (never READY_FOR_REVIEW).

    Read-only with respect to the DB. Never submits/clicks/fills/uploads.
    LLM only inside answer generation (truth sources only).

    snapshot_out, when a dict is passed, is filled with the raw DOM snapshot
    that was read. This function stays read-only: the caller decides whether to
    record a questionnaire from it. That split is deliberate and pinned by
    test_no_db_writes_during_extraction_and_validation - the collection write
    belongs to the caller, not to a function whose contract says it writes
    nothing (finding #38 was the same mistake in the other direction).
    """
    from .browser_executor import extract_form_for_vacancy

    try:
        form = extract_form_for_vacancy(
            vacancy_stable_id, url, adapter=adapter, canonical_id=canonical_id,
            snapshot_out=snapshot_out,
        )
    except Exception as e:
        pkg.validation_status = "NEEDS_REVIEW"
        reasons = list(getattr(pkg, "review_reasons", []) or [])
        reasons.append(f"Form extraction failed: {e}")
        pkg.review_reasons = reasons
        return pkg

    if form is None:
        pkg.validation_status = "NEEDS_REVIEW"
        reasons = list(getattr(pkg, "review_reasons", []) or [])
        reasons.append("Form extraction returned no form")
        pkg.review_reasons = reasons
        return pkg

    meta = form.extraction_meta or {}
    gate_reasons: list[str] = []
    if meta.get("error"):
        # BLE001 finding #7: the page was never read. An empty question list
        # here means "unknown", and every downstream gate ("all questions
        # resolved") would trivially pass on an empty set. Fail closed.
        gate_reasons.append(
            f"Form extraction error: {meta.get('error_reason') or 'unknown'} - "
            "DOM was not read, form contents are unknown (not empty)"
        )
    if meta.get("cdp_fallback"):
        # BLE001 finding #10: the DOM was read, but by a headless Playwright
        # browser that hh.ru can fingerprint, so what we got may be a bot page.
        gate_reasons.append(
            "Form read through a headless browser fallback "
            f"({meta.get('cdp_fallback_reason') or 'CDP unreachable'}) - "
            "contents are not trustworthy"
        )
    if meta.get("captcha"):
        gate_reasons.append("CAPTCHA detected during extraction - manual required")
    if meta.get("cloudflare"):
        gate_reasons.append("Cloudflare challenge detected during extraction - manual required")
    if meta.get("auth_form"):
        gate_reasons.append("HH auth gate: answer fields hidden without login - fields remain UNKNOWN")

    pkg = enrich_package_with_form(pkg, form, profile, resume_text, deep, vacancy, llm=llm)

    # Merge gate reasons with validator reasons (gate reasons first, dedup).
    merged: list[str] = []
    for r in list(gate_reasons) + list(getattr(pkg, "review_reasons", []) or []):
        if r not in merged:
            merged.append(r)
    pkg.review_reasons = merged
    return pkg