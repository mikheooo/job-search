# T8 — обратная генерация профиля и снятие дубликата

**Дата:** 2026-10-02. **Путь:** А (KB становится мастером) — выбран Мишей.
**Ничего наружу не отправлялось, `git push` не делался.**

---

## 1. Главная находка: приёмка в исходной формулировке была недостижима

План требовал: `candidate-profile render --dry-run` даёт **пустой диф** против текущего
`candidate_profile.json` — «то есть KB описывает профиль полностью».

Замер до всякой правки (`tools/.t8/coverage_map.py`, вывод —
`tools/.t8/coverage_map_20261002.txt`) показал, что это неверно. Из **37 полей** профиля
KB отдавал:

| Состояние | Сколько | Какие |
|---|---|---|
| напрямую | **3** | `name`, `email`, `languages` |
| выводимо правилом | **3** | `skills`, `domain_years`, `provenance` |
| частично | **8** | `core_skills`, `secondary_skills`, `transferable_skills`, `alternative_roles`, `minimum_salary`, `salary_currency`, `remote_required`, `must_avoid_conditions` |
| **отсутствует** | **23** | `target_roles`, `role_priorities`, `excluded_*`, `phone_ru/th`, `linkedin`, `github`, `portfolio`, `skill_confidence`, `seniority_range`, `employment_types`, `allowed_locations`, `years_experience`… |

Причина структурная, а не «забыли заполнить»: **KB описывает резюме** — роли,
обязанности, проекты, навыки, образование. **Профиль наполовину состоит из настроек
поиска** — какие роли искать, что исключать, минимальная зарплата, типы занятости.
Из резюме это не выводится и выводиться не может.

Дополнительно проверено:

- **В резюме нет телефонов, LinkedIn, GitHub и портфолио** — только имя, email, Telegram,
  Thailand, Remote (`resume.md:4`). Эти поля неоткуда взять, кроме профиля.
- **`skills` не покрыт даже как навыки:** в профиле `core_skills` содержит `active directory`,
  `automation`, `telegram`, которых в KB как skill-claims нет (Active Directory встречается
  только в тексте обязанности).
- **Две копии профиля идентичны — 0 различий.** `kb_verify` уже ловил это правилом
  `profile_copy_drift`: «`DEFAULT_PROFILE_PATHS` loads the first match, so the second copy is
  either dead weight or a trap».

---

## 2. Решение: предпочтения — отдельная секция KB, не claims

Путь А означает «KB — мастер». Но способ хранения выбран сознательно **не** через claims.

**Почему не claims.** Claim — это факт, прочитанный из источника и проверяемый против него:
у него есть `evidence`, `in_source` и участие в инвариантах `merge_claims` (слияние не
удаляет, ушедший факт → `in_source=false`, правка источника даёт новый `claim_id`). Для
предпочтения ответ на вопрос «это ещё есть в резюме?» — всегда «его там никогда не было».
Затащить предпочтения в claims значило бы применить к ним все инварианты T2, где они
бессмысленны, и получить сотни записей вместо тридцати пяти полей.

**Что сделано.** В `CandidateKnowledgeBase` добавлена секция `preferences: dict`. Она:
- не участвует в `merge_claims` (тот работает только с `self.claims`);
- не имеет статусов, evidence и `in_source` — их неоткуда взять;
- отсутствует в старом KB и читается как `{}` — обратная совместимость сохранена,
  `kb_format_version` оставлен `1` (поле необязательное).

---

## 3. Что изменено

| Файл | Что |
|---|---|
| `ai_assistant/candidate/kb.py` | секция `preferences` в `__init__` / `from_dict` / `to_dict` |
| `ai_assistant/candidate/profile_render.py` | **новый**, ~330 строк: рендер, диф, fail-closed, CLI-функция |
| `ai_assistant/candidate_profile.py` | `DEFAULT_PROFILE_PATHS` — 3 записи → 1 (две из трёх указывали на один и тот же файл) |
| `ai_assistant/cli.py` | команда `candidate-profile render` (байтовый патчер, 36 строк) |
| `tests/test_profile_render.py` | **новый**, 24 теста |
| `tests/test_candidate_kb.py`, `test_candidate_kb_verify.py`, `test_candidate_audit.py`, `test_candidate_interview.py`, `test_candidate_resume_parser.py` | удалённый файл убран из списков «защищённых» |
| `tools/t73_apply_profile.py` | мёртвый путь убран из `DEFAULT_PATHS` (цикл падал бы на несуществующем файле) |

**Удалён** `ai_assistant/candidate_profile.json` — бэкап в
`tools/.t8/candidate_profile_copy.bak-20261002-1428-before-t8`.

Служебное (вне git): `tools/.t8/coverage_map.py`, `import_preferences.py`,
`apply_cli_command.py`, `negative_controls.py`, бэкапы KB и профиля.

---

## 4. Импорт предпочтений — отдельный явный шаг

Фикс, срабатывающий в момент обнаружения, не наполняет уже накопленные данные. Поэтому
перенос 35 полей из профиля в KB — **отдельный скрипт** `tools/.t8/import_preferences.py`:
сначала dry-run с полным списком изменений, запись только по `--apply`, отказ, если
профиль чего-то не содержит.

```
kb_version 27 -> 28, claims 122 (не тронуты), preferences 0 -> 35
```

---

## 5. Приёмка

```
$ python -m ai_assistant.cli candidate-profile render --dry-run
profile matches the knowledge base: 37 fields, no differences.
  kb:      ...\candidate_kb.json
  profile: candidate_profile.json
```

**Диф пуст.** Живой KB воспроизводит живой профиль поле в поле.

Зафиксировано тестом `test_the_live_kb_reproduces_the_live_profile` (read-only).

---

## 6. Негативные контроли

`tools/.t8/negative_controls.py` — 4 мутации, каждая ломает одно свойство намеренно.
Файл восстанавливается побайтово после каждой, восстановление проверяется хешем и чистым
перепрогоном.

```
CAUGHT  drop a field from FILE_EXCLUDED_FIELDS              2 failed
CAUGHT  make the render fall back to a default instead of refusing   3 failed
CAUGHT  let transferable_skills collapse onto secondary_skills       1 failed
CAUGHT  make the diff always report agreement               2 failed

ok  file restored byte-for-byte
ok  clean re-run: 24 passed
all 4 mutations caught
```

**Первая версия контроля была негодной, и это стоит записать.** Мутация «заменить
`kb.preferences[field]` на `.get(field, [])`» **не поймалась**: fail-closed держит не цикл,
а проверка `missing_fields` перед ним. Мутация не ломала поведение — и зелёный набор был
прав. Понадобилось ломать **обе** половины (снять проверку и смягчить цикл), чтобы
контроль стал настоящим. Негативный контроль, который не меняет поведение, ничего не
доказывает — ровно как гейт, молчащий на хорошей нагрузке.

---

## 7. Тесты и ruff

```
tests/test_profile_render.py                     24 passed
весь затронутый набор                           364 passed, 0 failed, 0 errors (48.4 с)
```

Затронутый набор: `test_profile_render`, `test_candidate_kb`, `test_candidate_kb_verify`,
`test_candidate_profile`, `test_stage87_candidate_profile_calibration`,
`test_stage16_integrity_cli`, `test_stage30c_cli_wiring`, `test_stage30c_list_cli`,
`test_candidate_audit`, `test_candidate_interview`, `test_candidate_resume_parser`.

**ruff — паритет по правилам против снимка, дельта 0.** Снимки взяты из индекса git
(`git show :ai_assistant/...`, версия до T8) и из бэкапа для `cli.py`:

```
cli.py               снимок 116 (71 BLE001 + 45)  ->  текущий 45
kb.py                без изменений
candidate_profile.py без изменений
profile_render.py    All checks passed
```

Разница по `cli.py` — **артефакт пути**: `per-file-ignores` в `ruff.toml:518` глушит BLE001
для `ai_assistant/cli.py`, а снимок лежит в `tools/.t8/ruffbase/` и под правило не попадает.
По правилам разница нулевая.

---

## 8. Что оказалось не так, как планировалось

1. **Приёмка была недостижима** — см. §1. Понадобилось расширить KB, а не только написать
   рендер. Это уже не «одна задача», а ревизия KB плюс рендер.
2. **Предпочтения не пошли в claims** — см. §2.
3. **`transferable_skills` терялся бы при рендере через модель.** `CandidateProfile.__post_init__`
   (строки 134–144) делает `transferable_skills` алиасом `secondary_skills`, а живой файл
   держит **разные** списки. Значит `from_dict(...).to_dict()` молча перезаписал бы один
   другим. Рендер пишет напрямую из KB, минуя модель, — иначе это была бы потеря данных.
   Это существующий баг модели, не мой; зафиксирован тестом
   `test_transferable_skills_survive_the_render`.
4. **`location_constraints` — зеркало `allowed_locations`** (`candidate_profile.py:168-169`),
   а `industries` пуст и не читается никем. Оба поля есть в модели, но профиль-файл их не
   несёт, поэтому рендер их не пишет — явным списком `FILE_EXCLUDED_FIELDS`, а не «забыли».
5. **Формат файла профиля воспроизвести нельзя.** Он накапливался построчными правками
   (`tools/t73_apply_profile.py` пишет `write_bytes`, не переформатирует), поэтому вложенные
   списки в нём однострочные, а верхнеуровневые — многострочные. Рендер пишет своим
   форматом, но **при пустом дифе файл не трогает вообще** — так приёмка «пустой диф»
   остаётся честной, а не «диф пуст, но файл переформатирован».

---

## 9. Что осталось

1. **Правка предпочтений.** Сейчас `kb.preferences` заполнен импортом, но команды «поменять
   предпочтение» нет — только руками в JSON. Это следующий логичный шаг, если KB должна быть
   мастером не только на бумаге.
2. **Полный `tests/` — вердикт получен** (`tools/.t8/junit_full_t8_20261002.xml`, 3781.6 с):
   **3360 тестов, 9 падений, 0 ошибок, 0 пропущено.** Ни одно из 9 не относится к T8:
   - 2 в `tests/gig/` — проект `gig`, вне области T8;
   - `test_report_line_refs::test_the_report_has_no_new_symbol_mismatches` — красный и на
     чистом HEAD (ратчет `docs/ble001_triage.md`);
   - 3 в `tests/test_t78_search_only.py` — известные красные с T7.26.2, не регрессия;
   - `test_candidate_master_resume::test_a_project_sync_does_not_touch_resume_or_interview_claims`
     — внешнее изменение `C:\Users\Misha\reels_bot\README.md` (2026-10-02 15:16:18) сдвинуло
     `source_sha256`, `sync_project_claims` штатно обновил claim (разбор — `docs/t729_report.md` §6);
   - `test_t726_search_only_headless` и `test_t77_geo_context` — **загрязнение**: прогон
     стартовал до правок T7.29, оба теста используют `inspect.getsource` и перечитывают
     изменённый файл со старыми номерами строк.
   Регрессий от T8 нет; чистый прогон после правок T7.29 —
   `tools/.t729/junit_full_t729_20261002_clean.xml`: **3380 тестов, 2 падения, 0 ошибок** —
   оба падения известные (`test_candidate_master_resume` из-за внешнего `reels_bot/README.md`
   и ратчет `test_report_line_refs`), ни одного своего.
3. **Коммит не сделан.** `git push` запрещён. Рабочее дерево содержит 400+ чужих файлов в
   индексе — коммитить нужно отдельной задачей, разобрав индекс.
4. **`cli.py` переписывается параллельной сессией.** Правка внесена байтовым патчером при
   совпадении хеша `dcd39c11…`; после неё файл стал `e0921aa1b62276f1…` (316808 байт).
   Перед следующей работой с файлом сверять хеш заново.
