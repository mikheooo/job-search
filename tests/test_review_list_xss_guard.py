"""Regression guards for the stored XSS in the review-list dashboard (audit F3).

Untrusted board text reaches the dashboard through two layers, and both are
pinned here:

1. ``ai_assistant/ui/static/index.html`` must escape every review-list field
   (and the neighbouring vacancies / queue / package panels) before it is
   interpolated into ``innerHTML``, and must route hrefs through ``safeUrl()``
   so only absolute http(s) URLs are followed -- ``javascript:``, ``data:``,
   protocol-relative and relative values all degrade to ``'#'``.
2. ``ai_assistant/adapters/telegram_channels.py`` must decode HTML entities
   *before* stripping tags.  The old order (strip, then unescape) turned a post
   spelling ``&lt;img src=x onerror=...&gt;`` into a live ``<img onerror>``
   string in ``vacancies.title``.

The static checks run everywhere.  The helper behaviour and the URL-scheme
allow-list are additionally executed for real in node when it is installed
(the same optional-node pattern as ``tests/test_interview_pipeline.py``); if
node is missing those checks are skipped, not silently passed.

Under the DSH Windows sandbox run the suite through
``.venv\\Scripts\\python.exe artifacts\\audit-20261019\\pytest_runner.py ...``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path("ai_assistant/ui/static/index.html")
NODE = shutil.which("node")

HELPERS_START = "// --- html-safety helpers"
HELPERS_END = "// --- end html-safety helpers"

# Fields that come from external boards and used to be spliced raw into
# innerHTML.  ``salary_*`` is not listed because it is interpolated inside an
# escapeHtml() template; the specific assertions below cover it.
EXTERNAL_ITEMS_FIELDS = (
    "title", "url", "company", "location", "source", "category_label",
    "category", "band", "direction", "timezone_details", "timezone", "age_days",
)


def _index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _review_list_body() -> str:
    html = _index_html()
    start = html.find("async function loadReviewList")
    end = html.find("async function loadStats")
    if start == -1 or end == -1 or end <= start:
        pytest.fail(
            "index.html no longer contains loadReviewList/loadStats in the "
            "expected shape -- the XSS guard below cannot be checked"
        )
    return html[start:end]


def _safety_helpers_source() -> str:
    html = _index_html()
    if HELPERS_START not in html or HELPERS_END not in html:
        pytest.fail(
            "index.html has no html-safety helper block (escapeHtml/safeUrl): "
            "external board text is being rendered into innerHTML unescaped"
        )
    remainder = html.split(HELPERS_START, 1)[1]
    # drop the rest of the marker comment line, then keep everything up to the
    # closing marker (the marker text itself is the final comment).
    remainder = remainder.split("\n", 1)[1]
    return remainder.split(HELPERS_END, 1)[0]


def _js_function(name: str) -> str:
    """Extract one JS function body by matching its braces (tests only)."""
    html = _index_html()
    marker = f"function {name}("
    start = html.find(marker)
    if start == -1:
        pytest.fail(f"index.html no longer defines {name}()")
    brace = html.find("{", start)
    depth = 0
    for pos in range(brace, len(html)):
        char = html[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start:pos + 1]
    pytest.fail(f"unbalanced braces around {name}() in index.html")


# --- static: the dashboard escapes external text ---------------------------

def test_load_review_list_escapes_title_url_company_and_location():
    body = _review_list_body()

    assert "escapeHtml(it.title)" in body
    assert "escapeHtml(safeUrl(it.url))" in body
    assert "escapeHtml(it.company" in body
    assert "escapeHtml(it.location" in body

    # The exact raw interpolations the audit demonstrated must be gone.
    assert "${it.title}" not in body
    assert "${it.url}" not in body
    assert "${it.company}" not in body
    assert "${it.location}" not in body
    assert 'href="${it.url}"' not in body


def test_load_review_list_has_no_raw_interpolation_of_external_fields():
    body = _review_list_body()
    for field in EXTERNAL_ITEMS_FIELDS:
        # Any `${... it.<field> ...}` that is not immediately wrapped by
        # escapeHtml/safeUrl would let markup through.
        raw = re.compile(
            rf"\$\{{(?!escapeHtml|safeUrl)[^}}]*\bit\.{field}\b"
        )
        match = raw.search(body)
        assert match is None, (
            f"review-list interpolates it.{field} without escapeHtml/safeUrl: "
            f"{match.group(0) if match else ''}"
        )


def test_load_review_list_escapes_summary_bands_and_lists():
    body = _review_list_body()

    # category summary labels and counters come from the API response.
    assert "escapeHtml(labels[k] || k)" in body
    assert "escapeHtml(v)" in body
    # open_questions / band_reasons are derived from external text.
    assert "escapeHtml(q)" in body
    assert "escapeHtml(r)" in body
    # salary currency is an external string.
    salary_line = next(line for line in body.splitlines() if "escapeHtml(`${it.salary_min" in line)
    assert "it.salary_currency" in salary_line
    # timezone details / direction / band labels are escaped as well.
    assert "escapeHtml(it.timezone_details" in body
    assert "escapeHtml(it.direction)" in body
    assert "escapeHtml(it.band" in body
    assert "escapeHtml(it.category_label" in body


def test_load_review_list_error_path_escapes_exception_text():
    body = _review_list_body()
    assert "escapeHtml(e)" in body, (
        "the catch branch writes the exception into innerHTML unescaped"
    )


def test_vacancies_queue_and_package_panels_are_escaped_too():
    """Same API-sourced fields, neighbouring insertions (audit lines :565/:583/:639)."""
    vacancies = _js_function("loadVacancies")
    assert "escapeHtml(v.title)" in vacancies
    assert "escapeHtml(v.company" in vacancies
    assert "escapeHtml(v.location" in vacancies
    assert "escapeHtml(v.source)" in vacancies
    assert "escapeHtml(safeUrl(v.job_url))" in vacancies
    assert 'href="${v.job_url' not in vacancies
    assert "escapeHtml((v.eligibility_reasons || []).join('; '))" in vacancies

    queue = _js_function("loadQueue")
    assert "escapeHtml(it.title)" in queue
    assert "escapeHtml(it.company" in queue
    assert "escapeHtml(it.review_status)" in queue
    assert "escapeHtml(safeUrl(it.job_url))" in queue
    assert 'href="${it.job_url' not in queue

    package = _js_function("openPackageModal")
    assert "safeUrl(d.vacancy?.job_url)" in package
    assert "escapeHtml(q.label || q.name)" in package
    assert "escapeHtml(q.resolved_value" in package


def test_inline_handlers_do_not_splice_ids_into_javascript():
    """A stable id inside onclick="fn('${id}')" cannot be protected by HTML escaping.

    Quotes in an HTML-escaped attribute are decoded back to JS source, so the
    ids are passed through data-* attributes instead of an inline JS literal.
    """
    html = _index_html()
    assert "onclick=\"openPackageModal('${" not in html
    assert html.count("onclick=\"openPackageModal(this.dataset.stableId)\"") == 3


# --- executing the helpers (node, optional) ---------------------------------

def _node_report(tmp_path, js: str) -> dict:
    script = tmp_path / "check.js"
    script.write_text(js, encoding="utf-8")
    out_path = tmp_path / "out.json"
    err_path = tmp_path / "stderr.txt"
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        proc = subprocess.run(
            [NODE, str(script)],
            check=False,
            cwd=str(tmp_path), stdout=out, stderr=err, timeout=60,
        )
    assert proc.returncode == 0, f"node failed: {err_path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
    return json.loads(out_path.read_text(encoding="utf-8").strip().splitlines()[-1])


@pytest.fixture(scope="module")
def helper_report(tmp_path_factory):
    if not NODE:
        pytest.skip("node is required to execute the dashboard helpers")
    tmp_path = tmp_path_factory.mktemp("xss-helpers")
    js = _safety_helpers_source() + r"""
const cases = {
  https: safeUrl('https://hh.ru/vacancy/123?utm=1&x=2'),
  http: safeUrl('http://example.com/jobs/1'),
  javascript: safeUrl('javascript:alert(document.cookie)'),
  javascript_upper: safeUrl('JaVaScRiPt:alert(1)'),
  javascript_spaced: safeUrl('  javascript:alert(1)  '),
  javascript_newline: safeUrl('java\nscript:alert(1)'),
  data: safeUrl('data:text/html,<script>alert(1)</script>'),
  vbscript: safeUrl('vbscript:msgbox(1)'),
  protocol_relative: safeUrl('//evil.example/x'),
  relative: safeUrl('/api/review-list'),
  file: safeUrl('file:///C:/Windows/win.ini'),
  empty: safeUrl(''),
  nullish: safeUrl(null),
};
const html = escapeHtml('<img src=x onerror="alert(1)"> & \'q\'');
console.log(JSON.stringify({ cases, html }));
"""
    return _node_report(tmp_path, js)


def test_safe_url_allows_only_http_and_https(helper_report):
    cases = helper_report["cases"]
    assert cases["https"].startswith("https://hh.ru/vacancy/123")
    assert cases["http"].startswith("http://example.com/jobs/1")

    for name in (
        "javascript", "javascript_upper", "javascript_spaced",
        "javascript_newline", "data", "vbscript", "protocol_relative",
        "relative", "file", "empty", "nullish",
    ):
        assert cases[name] == "#", f"safeUrl accepted {name}: {cases[name]!r}"


def test_escape_html_neutralises_markup(helper_report):
    escaped = helper_report["html"]
    assert "<" not in escaped and ">" not in escaped
    assert '"' not in escaped and "'" not in escaped
    assert "&lt;img" in escaped
    assert "&amp;" in escaped
    assert "&#39;" in escaped
    # the attack text itself is preserved as inert text, not dropped silently
    assert "onerror" in escaped


def test_safe_url_allow_list_is_pinned_in_the_source():
    """The scheme check must stay an allow-list of http/https, not a deny-list."""
    src = _safety_helpers_source()
    assert "function safeUrl" in src
    assert "^https?://" in src.replace("\\/", "/")
    assert "parsed.protocol !== 'http:'" in src
    assert "parsed.protocol !== 'https:'" in src
    assert "return '#'" in src


# --- the adapter must decode first, then strip ------------------------------

def test_clean_post_html_removes_encoded_markup_but_keeps_text():
    from ai_assistant.adapters.telegram_channels import clean_post_html

    out = clean_post_html("&lt;img src=x onerror=alert(document.cookie)&gt; DevOps удалённо")
    assert "<" not in out and ">" not in out
    assert "DevOps" in out and "удалённо" in out

    script = clean_post_html("&lt;script&gt;alert('xss')&lt;/script&gt; Python developer")
    assert "<" not in script and ">" not in script
    assert "alert('xss')" in script, "tag text is kept as inert text, not dropped"
    assert "Python developer" in script

    # a raw tag in the fragment is stripped just like an encoded one
    mixed = clean_post_html(
        "<img src=x onerror=alert(document.cookie)> DevOps удалённо "
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    assert "<" not in mixed and ">" not in mixed, mixed
    assert "DevOps" in mixed and "удалённо" in mixed


def test_clean_post_html_survives_the_strip_then_unescape_chain():
    """The exact chain from the audit: ``&lt;img onerror&gt;`` must not revive."""
    from ai_assistant.adapters.telegram_channels import clean_post_html

    for payload in (
        "&lt;img src=x onerror=alert(1)&gt; remote python",
        "&lt;script&gt;alert(1)&lt;/script&gt; remote python",
        "&#60;img src=x onerror=alert(1)&#62; remote python",
        "&amp;lt;img src=x onerror=alert(1)&amp;gt; remote python",
    ):
        out = clean_post_html(payload)
        assert "<" not in out and ">" not in out, (payload, out)
        assert "remote python" in out, (payload, out)


def test_clean_post_html_preserves_plain_text_and_ampersands():
    from ai_assistant.adapters.telegram_channels import clean_post_html

    out = clean_post_html("Python &amp; Linux, удалённо. Зарплата: 200 000 ₽")
    assert out == "Python & Linux, удалённо. Зарплата: 200 000 ₽"

    # one decoding pass only: double-encoded markup stays displayable text
    double = clean_post_html("&amp;lt;img src=x onerror=alert(1)&amp;gt; python")
    assert "&lt;img" in double
    assert "<" not in double and ">" not in double


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_fetch_channel_never_returns_executable_markup(monkeypatch):
    """End-to-end: a hostile post becomes inert title/description text."""
    from ai_assistant.adapters import telegram_channels as tg

    page = """
    <div class="tgme_widget_message" data-post="remoteit/12345">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        &lt;img src=x onerror=alert(document.cookie)&gt; Удаленная работа Python developer
        &lt;script&gt;alert(1)&lt;/script&gt; 200 000 RUB
        <br>Контакты: @recruiter
      </div>
    </div>
    """
    monkeypatch.setattr(
        tg.urllib.request, "urlopen", lambda req, timeout=12: _FakeResponse(page)
    )

    vacancies = tg.TelegramChannelsAdapter(channels=["remoteit"]).fetch_channel("remoteit")
    assert vacancies, "the hostile post still has to be parsed, only inertly"
    vac = vacancies[0]
    assert "Python" in vac.title
    assert "<img" not in vac.title and "&lt;img" not in vac.title
    assert "<script" not in vac.title
    for text in (vac.title, vac.company or "", vac.description or ""):
        assert "<img" not in text, text
        assert "<script" not in text, text
    assert "@recruiter" in (vac.description or "")
