# Руководство по работе с Git Worktree в Job-Search

## Зачем нужны worktree в проекте

В проекте часто требуется:
1. **Проверять тесты на чистом HEAD**: как зафиксировано в `docs/project_map_for_space.md`, при падении тестов важно быстро перепроверить их поведение на чистом HEAD, чтобы отделить предсуществующие поломки от текущих правок.
2. **Параллельная разработка и фичи**: работа над адаптерами вакансий, формами откликов или агентами без необходимости переключать и загрязнять основное рабочее дерево.
3. **Изоляция экспериментов**: возможность запускать и отлаживать код в чистом независимом каталоге.

---

## Автоматическая настройка в приложении Paseo (`paseo.json`)

При создании изолированного воркспейса (режим **Worktree**) в приложении **Paseo**, приложение автоматически читает конфигурационный файл [`paseo.json`](../paseo.json) в корне репозитория и запускает хук `worktree.setup`.

Конфигурация в `paseo.json`:
```json
{
  "$schema": "https://paseo.dev/schema.json",
  "worktree": {
    "setup": [
      "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./scripts/setup_worktree.ps1"
    ]
  },
  "scripts": {
    "test": {
      "command": ".\\.venv\\Scripts\\pytest.exe tests -q"
    },
    "ui": {
      "type": "service",
      "command": ".\\.venv\\Scripts\\python.exe -m ai_assistant.cli ui --host 0.0.0.0 --port 8000",
      "port": 8000
    }
  }
}
```

Когда Paseo создаёт новое worktree:
1. Paseo автоматически создаёт директорию worktree и ветку.
2. Передаёт путь исходного репозитория в переменную окружения `$env:PASEO_SOURCE_CHECKOUT_PATH`.
3. Запускает команду `worktree.setup`, которая выполняет `setup_worktree.ps1`:
   - Создаёт виртуальное окружение `.venv` (через `uv` за пару секунд или `python -m venv`).
   - Устанавливает все зависимости из `requirements.txt` и `requirements-dev.txt`.
   - Загружает браузер Playwright Chromium.
   - Копирует локальные конфигурационные файлы (`candidate_profile.json`, `ai_assistant\.env`, `.env`) из исходного репозитория.
   - Проверяет импорты (`ai_assistant`, `pytest`, `pydantic`).

---

## Быстрый старт через CLI

Управление осуществляется через единый скрипт `scripts/worktree.ps1` (PowerShell) или `scripts/worktree.bat` (CMD / терминал Windows):

### 1. Создание нового worktree с автоматической подготовкой

```powershell
# Создать новую ветку и полностью изолированный worktree:
.\scripts\worktree.ps1 new feature/my-feature -b

# Быстрый легковесный worktree с общим .venv родительского репозитория (мгновенно через NTFS Junction):
.\scripts\worktree.ps1 new test-clean-head -SharedVenv

# Создать worktree с автоматическим прогоном дымовых тестов:
.\scripts\worktree.ps1 new feature/remote-jobs -b -RunTests
```

Команда автоматически:
1. Выполняет `git worktree add`.
2. Создаёт виртуальное окружение `.venv` (через быстрый `uv` или системный `python -m venv`).
3. Устанавливает все зависимости из `requirements.txt` и `requirements-dev.txt`.
4. Устанавливает браузер Playwright Chromium (`playwright install chromium`).
5. Копирует локальные конфигурационные файлы из родительского репозитория (`candidate_profile.json`, `ai_assistant\.env`, `.env`).
6. Выполняет sanity check (проверка импорта `ai_assistant`, `pytest`, `pydantic`).

### 2. Подготовка существующего или текущего worktree

Если вы уже перешли в директорию worktree или создали её вручную:

```powershell
# Внутри директории worktree:
.\scripts\worktree.ps1 setup

# Либо указав путь к целевому worktree:
.\scripts\worktree.ps1 setup -Path "C:\path\to\worktree"

# С флагом быстрого подключения venv:
.\scripts\worktree.ps1 setup -SharedVenv
```

### 3. Список всех worktree и диагностика их готовности

```powershell
.\scripts\worktree.ps1 list
```

Пример вывода:
```
# Branch              Venv             Profile Env Status          Path
- ------              ----             ------- --- ------          ----
1 chore-ruff-cleanup  Isolated (.venv) Yes     Yes Main Repository C:/Users/Misha/Documents/job-search
2 feature/my-feature  Isolated (.venv) Yes     Yes Ready           C:/Users/Misha/Documents/job-search-feature-my-feature
```

### 4. Безопасное удаление worktree

```powershell
# Удалить worktree по ветке или пути:
.\scripts\worktree.ps1 remove feature/my-feature

# Принудительно удалить (даже если есть незакоммиченные файлы) и удалить саму ветку:
.\scripts\worktree.ps1 remove feature/my-feature -Force -DeleteBranch
```

---

## Настройка Git-алиасов

Для максимального удобства можно зарегистрировать короткие команды `git wt-*`:

```powershell
.\scripts\register_worktree_git_aliases.ps1
```

После регистрации доступны стандартные команды git:
- `git wt-new <branch> [-b]` — создать и подготовить worktree
- `git wt-setup` — настроить текущий worktree
- `git wt-list` — список всех worktrees со статусом окружений
- `git wt-rm <target> [-Force] [-DeleteBranch]` — удалить worktree

Для удаления алиасов выполните:
```powershell
.\scripts\register_worktree_git_aliases.ps1 -Unregister
```

---

## Доступные опции команд

| Параметр | Описание |
|---|---|
| `-b`, `-CreateBranch` | Создать новую git-ветку при вызове `new` |
| `-BaseRef <ref>` | Базовая точка ветвления (по умолчанию `HEAD`) |
| `-SharedVenv` | Подключить `.venv` родительского репозитория через NTFS Junction (0.1 сек) |
| `-UsePip` | Принудительно использовать `pip` вместо `uv` |
| `-SkipDev` | Не устанавливать `requirements-dev.txt` (только базовые зависимости) |
| `-SkipPlaywright` | Не устанавливать браузер Playwright Chromium |
| `-SkipConfigCopy` | Не копировать `candidate_profile.json` и `.env` |
| `-RunTests` | Запустить быстрые дымовые тесты `pytest` после подготовки |
| `-Force` | Принудительное удаление worktree при вызове `remove` |
| `-DeleteBranch` | Удалить ветку при удалении worktree |

---

## Состав скриптов в репозитории

- `scripts/worktree.ps1` — единый CLI-диспетчер.
- `scripts/worktree.bat` — обёртка для CMD и терминала Windows.
- `scripts/setup_worktree.ps1` — процедура настройки `.venv`, зависимостей, Playwright и конфигов.
- `scripts/new_worktree.ps1` — создание worktree и запуск его настройки.
- `scripts/list_worktrees.ps1` — диагностический вывод всех worktree в репозитории.
- `scripts/remove_worktree.ps1` — безопасное удаление worktree и очистка ссылок git.
- `scripts/register_worktree_git_aliases.ps1` — регистрация git-алиасов в `.git/config`.
- `tests/test_worktree_scripts.py` — автоматические тесты работоспособности скриптов.
