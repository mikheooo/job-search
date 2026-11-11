"""Dynamic Resume, Experience Tailoring & Interview Preparation Engine."""
from ai_assistant.tailoring.analyzer import VacancyDomain, analyze_vacancy
from ai_assistant.tailoring.resume_tailor import (
    TailoredApplicationPackage,
    tailor_application_for_vacancy,
)

__all__ = [
    "VacancyDomain",
    "analyze_vacancy",
    "TailoredApplicationPackage",
    "tailor_application_for_vacancy",
]
