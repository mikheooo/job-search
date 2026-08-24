from . import config
import logging

def check_vacancy(title: str, description: str, salary: int = None) -> tuple[bool, str]:
    """
    Проверяет вакансию по стоп-словам, обязательным словам и зарплате.
    Возвращает (Passed, Reason)
    """
    text_to_check = f"{title} {description}".lower()
    
    # 1. Проверка стоп-слов
    for word in config.STOP_WORDS:
        if word in text_to_check:
            return False, f"Stop-word found: {word}"
            
    # 2. Проверка обязательных слов (если заданы)
    if config.REQUIRED_WORDS:
        has_required = any(word in text_to_check for word in config.REQUIRED_WORDS)
        if not has_required:
            return False, "No required words found"
            
    # 3. Проверка минимальной зарплаты
    if config.MIN_SALARY > 0 and salary is not None:
        if salary < config.MIN_SALARY:
            return False, f"Salary {salary} < {config.MIN_SALARY}"
            
    return True, "Passed"
