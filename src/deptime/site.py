"""Rename-map pages generated from the index (DT-GTM-01): one static page per well-known migration, every row from
the same data the MCP tools serve (the index, or a curated hint that passed its checks).

  uv run python -m deptime.site build --out site/dist [--index PATH] [--base-url https://example.org]

Each page lists, between the newest release of the old series and the newest of the new one, the names and
arguments that went away and what the index says replaces them (the library's own text, a curated guide, or the same
object at another path), then the ones removed without a stated replacement. Library text is escaped and quoted as
data. Writes one page per migration, index.html (a short landing page for DepTime that lists them), llms.txt and,
with --base-url, sitemap.xml and each page's canonical URL. `.github/workflows/pages.yml` publishes them to GitHub
Pages after every index build (DT-GTM-02).
"""

from __future__ import annotations

import argparse
import builtins
import html
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from packaging.version import InvalidVersion, Version

from deptime.ingest.curated import hints_for
from deptime.models import HistoryEvent, Replacement
from deptime.query import EXPLAIN_MIN_CONFIDENCE, SEVERITY, DepTime

MAX_ROWS = 2000  # per section: past this a page is too long to read, filter or not
CHANGES = ("removed", "moved", "param_removed", "deprecated", "param_deprecated")
SOURCES = {
    "deprecation_message": "the library's deprecation text",
    "moved_heuristic": "the same object at another path",
    "curated": "the project's migration guide",
    "error_message": "the library's error message",
}


@dataclass(frozen=True)
class Range:
    package: str
    old: str  # version prefix of the old series, e.g. "1." or "0.3."
    new: str | None = None  # version prefix of the new series; None for the newest release


@dataclass(frozen=True)
class Migration:
    slug: str
    title: str
    ranges: tuple[Range, ...] = ()
    curated_from: str | None = None  # a distribution whose curated hints point into another one


MIGRATIONS = (
    Migration("mcp-1-to-2", "MCP Python SDK 1.x → 2.x", (Range("mcp", "1."),)),
    Migration(
        "langchain-0.3-to-1",
        "LangChain 0.3 → 1.x",
        (Range("langchain", "0.3."), Range("langchain-core", "0.3.")),
    ),
    Migration(
        "google-generativeai-to-google-genai",
        "google-generativeai → google-genai",
        curated_from="google-generativeai",
    ),
    Migration("pydantic-1-to-2", "pydantic v1 → v2", (Range("pydantic", "1.10.", "2."),)),
    Migration("openai-0-to-1", "openai 0.x → 1.x", (Range("openai", "0.28", "1."),)),
    Migration("httpx-0.27-to-0.28", "httpx 0.27 → 0.28", (Range("httpx", "0.27.", "0.28."),)),
    Migration("pandas-1-to-2", "pandas 1.x → 2.x", (Range("pandas", "1.", "2."),)),
    Migration("pandas-2-to-3", "pandas 2.x → 3.x", (Range("pandas", "2.", "3."),)),
    Migration("polars-0.20-to-1", "Polars 0.20 → 1.x", (Range("polars", "0.20.", "1."),)),
    Migration("polars-1-to-2", "Polars 1.x → 2.x", (Range("polars", "1.", "2."),)),
    Migration("gradio-3-to-4", "Gradio 3.x → 4.x", (Range("gradio", "3.", "4."),)),
    Migration("gradio-4-to-5", "Gradio 4.x → 5.x", (Range("gradio", "4.", "5."),)),
    Migration("gradio-5-to-6", "Gradio 5.x → 6.x", (Range("gradio", "5.", "6."),)),
    Migration("fastmcp-2-to-3", "FastMCP 2.x → 3.x", (Range("fastmcp", "2.", "3."),)),
    Migration("fastmcp-3-to-4", "FastMCP 3.x → 4.x", (Range("fastmcp", "3.", "4."),)),
    Migration("pydantic-ai-1-to-2", "Pydantic AI 1.x → 2.x", (Range("pydantic-ai-slim", "1.", "2."),)),
    Migration("huggingface-hub-0-to-1", "huggingface_hub 0.x → 1.x", (Range("huggingface-hub", "0.", "1."),)),
    Migration("pillow-9-to-10", "Pillow 9.x → 10.x", (Range("pillow", "9.", "10."),)),
    Migration("urllib3-1-to-2", "urllib3 1.x → 2.x", (Range("urllib3", "1.", "2."),)),
    Migration("marshmallow-3-to-4", "marshmallow 3.x → 4.x", (Range("marshmallow", "3.", "4."),)),
    Migration("werkzeug-2-to-3", "Werkzeug 2.x → 3.x", (Range("werkzeug", "2.", "3."),)),
    Migration("pypdf-3-to-4", "pypdf 3.x → 4.x", (Range("pypdf", "3.", "4."),)),
)
LISTED = 5  # migrations the landing page's description names before "and N more"

STYLE = """
:root{--bg:#fff;--surface:#f6f8fa;--raised:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--soft:#e6eaef;
--brand:#3776ab;--py:#ffd43b;--brand-soft:#eaf2fa;--new:#1a7f37;--new-bg:#dafbe1;--code:#eff2f5;--ink:#1f2328;--on-ink:#fff;
--shadow:0 1px 2px rgba(31,35,40,.06),0 12px 32px rgba(31,35,40,.08);color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--surface:#151b23;--raised:#151b23;--fg:#e6edf3;--muted:#9198a1;
--line:#30363d;--soft:#21262d;--brand:#79a8d4;--py:#d9b42c;--brand-soft:#16263a;--new:#3fb950;--new-bg:#12261a;--code:#1f252d;
--ink:#e6edf3;--on-ink:#0d1117;--shadow:0 0 0 1px #21262d,0 12px 32px rgba(0,0,0,.35);color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
code{font-size:.86em}
.wrap{max-width:72rem;margin:0 auto;padding:0 1.25rem}
.top{--shade:0;position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) calc(var(--shade) * 85%),transparent);backdrop-filter:saturate(calc(100% + var(--shade) * 80%)) blur(calc(var(--shade) * 16px));-webkit-backdrop-filter:saturate(calc(100% + var(--shade) * 80%)) blur(calc(var(--shade) * 16px));border-bottom:1px solid color-mix(in srgb,var(--soft) calc(var(--shade) * 100%),transparent)}
.top .wrap{display:flex;align-items:center;justify-content:space-between;height:3.5rem}
.brand{display:flex;align-items:center}.brand img{height:22px;width:auto;display:block}
.top nav a{color:var(--muted);margin-left:1.5rem;font-size:.92rem;font-weight:500}.top nav a:hover{color:var(--fg);text-decoration:none}
.hero{text-align:center;padding:5rem 0 1rem}
.halo{position:absolute;top:0;left:0;right:0;height:48rem;z-index:-1;pointer-events:none;background:radial-gradient(42% 46% at 26% 0,color-mix(in srgb,var(--brand) 22%,transparent),transparent),radial-gradient(36% 42% at 76% 8%,color-mix(in srgb,var(--py) 20%,transparent),transparent)}
.pill{display:inline-block;font-size:.78rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--brand);background:var(--brand-soft);padding:.3rem .8rem;border-radius:999px}
.hero h1{font-size:clamp(2.1rem,5.2vw,3.5rem);line-height:1.08;letter-spacing:-.03em;margin:1.1rem auto 1rem;max-width:18ch;font-weight:750}
.lede{font-size:1.18rem;color:var(--muted);max-width:40rem;margin:0 auto}
.actions{display:flex;flex-wrap:wrap;gap:.75rem;justify-content:center;margin-top:2.25rem}
.btn{display:inline-flex;align-items:center;gap:.5rem;height:2.75rem;padding:0 1.2rem;border-radius:8px;font-weight:600;font-size:.95rem;border:1px solid var(--line);color:var(--fg);background:var(--raised)}
.btn:hover{text-decoration:none;border-color:var(--muted)}
.btn.ink{background:var(--ink);border-color:var(--ink);color:var(--on-ink)}.btn.ink:hover{opacity:.9}
.cmd{display:inline-flex;align-items:center;gap:.6rem;height:2.75rem;padding:0 .35rem 0 1rem;border:1px solid var(--line);border-radius:8px;background:var(--surface);font-size:.92rem;max-width:100%}
.cmd code{font-size:inherit;white-space:nowrap;min-width:0;overflow-x:auto;scrollbar-width:none}.cmd code::-webkit-scrollbar{display:none}.cmd .p{color:var(--muted);user-select:none}
.copy{flex:none;border:0;background:transparent;color:var(--muted);cursor:pointer;padding:.35rem .6rem;border-radius:6px;font:600 .78rem/1 inherit;font-family:inherit}
.copy:hover{background:var(--soft);color:var(--fg)}
.demo{margin:3.5rem auto 0;max-width:47.5rem}
.demo img{display:block;width:100%;height:auto;border-radius:10px;box-shadow:var(--shadow)}
.demo figcaption{text-align:center;color:var(--muted);font-size:.85rem;margin-top:.9rem}
.block{padding:5.5rem 0 0}
.block h2{font-size:clamp(1.5rem,3vw,1.9rem);letter-spacing:-.02em;margin:0 0 .5rem;line-height:1.2}
.block .sub{color:var(--muted);margin:0 0 2rem;max-width:44rem;font-size:1.05rem}
.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fill,minmax(17rem,1fr))}
.card{display:block;border:1px solid var(--line);border-radius:12px;padding:1.35rem;background:var(--raised);color:inherit}
.card h3{font-size:1.05rem;margin:0 0 .45rem;letter-spacing:-.005em}.card p{margin:0;color:var(--muted);font-size:.95rem}
a.card{transition:border-color .15s,transform .15s,box-shadow .15s}a.card:hover{text-decoration:none;border-color:var(--brand);transform:translateY(-2px);box-shadow:var(--shadow)}
.ico{width:2.4rem;height:2.4rem;border-radius:9px;background:var(--brand-soft);color:var(--brand);display:grid;place-items:center;margin-bottom:1rem}
.ico svg{width:1.25rem;height:1.25rem}
.span{font-size:.8rem;color:var(--muted);margin-top:.15rem}
.stats{display:flex;gap:1.25rem;margin-top:1rem;font-size:.88rem;color:var(--muted)}.stats b{color:var(--fg);font-variant-numeric:tabular-nums;font-weight:650}
.band{border:1px solid var(--line);border-radius:16px;background:var(--surface);padding:2rem;display:grid;gap:1.5rem 2.5rem;grid-template-columns:1.2fr 1fr;align-items:center}
.band h2{margin-bottom:.4rem}.band p{color:var(--muted);margin:0}
.band>div{min-width:0}.band .cmd{width:100%;justify-content:space-between}
.foot{margin-top:6rem;border-top:1px solid var(--soft);padding:2rem 0 3rem;color:var(--muted);font-size:.86rem}
.foot .wrap{display:flex;flex-wrap:wrap;gap:.75rem 2rem;justify-content:space-between;align-items:center}
.foot nav a{color:var(--muted);margin-right:1.25rem}.foot nav a:hover{color:var(--fg)}
.crumbs{padding-top:2.25rem;font-size:.86rem;color:var(--muted)}.crumbs a{color:var(--muted)}.crumbs a:hover{color:var(--fg)}
.crumbs span{margin:0 .4rem;opacity:.6}
.maphead h1{font-size:clamp(1.8rem,4.2vw,2.6rem);letter-spacing:-.025em;line-height:1.12;margin:.8rem 0 .5rem;font-weight:750}
.maphead h1 small{display:block;font-size:.5em;font-weight:500;color:var(--muted);letter-spacing:-.005em;margin-top:.4rem}
.maphead .tag{font-size:.84rem;color:var(--muted)}
.maphead .intro{color:var(--muted);max-width:46rem;margin:1rem 0 0}
.tiles{display:flex;flex-wrap:wrap;gap:1rem;margin:1.75rem 0}
.tile{flex:1 1 13rem;max-width:20rem;border:1px solid var(--line);border-radius:12px;padding:1rem 1.25rem;background:var(--raised)}
.tile b{display:block;font-size:1.75rem;line-height:1.1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}.tile span{color:var(--muted);font-size:.88rem}
.cta{display:flex;flex-wrap:wrap;gap:1rem 2rem;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem;background:var(--brand-soft)}
.cta p{margin:0;max-width:34rem}.cta .cmd{background:var(--raised)}
.toolbar{position:sticky;top:3.5rem;z-index:10;background:var(--bg);display:flex;flex-wrap:wrap;gap:.5rem 1rem;align-items:center;padding:1rem 0;margin-top:1.5rem;border-bottom:1px solid var(--soft)}
.filter{flex:1 1 18rem;max-width:30rem;height:2.6rem;border:1px solid var(--line);border-radius:8px;padding:0 .9rem 0 2.4rem;color:var(--fg);font:inherit;font-size:.95rem;
background:var(--surface) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' fill='none' stroke='%238b949e' stroke-width='2' stroke-linecap='round' viewBox='0 0 24 24'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='m20 20-3.5-3.5'/%3E%3C/svg%3E") no-repeat .85rem center}
.filter:focus{outline:2px solid var(--brand);outline-offset:-1px;border-color:transparent}
.count{color:var(--muted);font-size:.86rem}
.changes h2{font-size:1.15rem;margin:2.25rem 0 .85rem;letter-spacing:-.01em}.changes h2 .n{color:var(--muted);font-weight:500;margin-left:.35rem}
.tw{border:1px solid var(--line);border-radius:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th{background:var(--surface);text-align:left;font-weight:600;font-size:.74rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:.65rem 1rem;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:.65rem 1rem;border-top:1px solid var(--soft);vertical-align:top}tbody tr:first-child td{border-top:0}
tbody tr:hover td{background:var(--surface)}
td code{background:var(--code);padding:.12rem .4rem;border-radius:5px;overflow-wrap:break-word;line-height:1.7;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.replaced td:nth-child(2) code{background:var(--new-bg);color:var(--new)}
td.v{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--muted)}
td.ev{color:var(--muted);font-size:.82rem;min-width:16rem;max-width:30rem}td.ev code{font-size:.92em;padding:.05rem .3rem}
.note{color:var(--muted);font-size:.84rem;margin:0}
.none{display:none;text-align:center;color:var(--muted);padding:3rem 0}
@media (max-width:720px){.hero{padding-top:3rem}.tile{flex:1 1 9rem;max-width:none;padding:.85rem 1rem}.tile b{font-size:1.45rem}.cta .cmd{width:100%;justify-content:space-between}.actions .cmd{width:100%;justify-content:space-between}.band{grid-template-columns:1fr;padding:1.5rem}.top nav a.hide-sm{display:none}.block{padding-top:4rem}}
"""

SCRIPT = """
// The header turns from clear to frosted over the first 320px of scrolling, in step with the page.
const bar=document.querySelector('.top'),edge=()=>bar.style.setProperty('--shade',Math.min(scrollY/320,1).toFixed(3));
addEventListener('scroll',edge,{passive:true});edge();
document.querySelectorAll('.copy').forEach(b=>b.addEventListener('click',async()=>{
try{await navigator.clipboard.writeText(b.dataset.copy);b.textContent='Copied';setTimeout(()=>{b.textContent='Copy'},1600)}catch(e){}}));
const f=document.getElementById('filter');
if(f){const rows=[...document.querySelectorAll('.changes tbody tr')],c=document.getElementById('count'),none=document.getElementById('none');
const run=()=>{const q=f.value.trim().toLowerCase();let n=0;
for(const r of rows){const hit=!q||r.textContent.toLowerCase().includes(q);r.hidden=!hit;if(hit)n++}
document.querySelectorAll('.changes').forEach(s=>{s.hidden=!!q&&!s.querySelector('tbody tr:not([hidden])')});
c.textContent=q?n+' of '+rows.length+' names match':rows.length+' names';none.style.display=q&&!n?'block':'none';
const u=new URL(location);q?u.searchParams.set('q',f.value):u.searchParams.delete('q');history.replaceState(null,'',u)};
f.value=new URLSearchParams(location.search).get('q')||'';f.addEventListener('input',run);run()}
"""

ICONS = {
    "line": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h7"/><circle cx="18" cy="17" r="3"/><path d="m20.2 19.2 1.8 1.8"/></svg>',
    "offline": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 4 6v6c0 4.4 3.4 8.3 8 9 4.6-.7 8-4.6 8-9V6z"/><path d="m9 12 2 2 4-4"/></svg>',
    "pr": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5v7M18 15.5V9a3 3 0 0 0-3-3h-4"/><path d="m13 4-2 2 2 2"/></svg>',
}

REPO = "https://github.com/cschmertz/deptime"
ACTION = "https://github.com/marketplace/actions/deptime-upgrade-check"
ACTION_DOCS = f"{REPO}/blob/main/docs/github-action.md"
BENCHMARK = f"{REPO}/blob/main/bench/README.md"
PYPI = "https://pypi.org/project/deptime/"
# A first-run result, fixed once scored (DT-QA-22), so the page can quote it without going stale.
PR_RESULTS = f"{REPO}/blob/main/bench/upgrade-check/prs/results-2026-11-16.md"
PR_SET = "264 real Dependabot and Renovate pull requests in open-source projects, collected before DepTime saw them"
PR_RESULT = (
    ("13 of 13", "pull requests that broke their project failed the check"),
    ("46 of 48", "broken uses of a library found, each with its line"),
    ("1 of 3,291", "uses that still worked flagged by mistake"),
)
# Copied next to the pages from the repository's docs/assets when it's there (a source checkout, as in pages.yml).
ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets"
ASSET_FILES = (
    "deptime-icon.svg",
    "deptime-wordmark-light.svg",
    "deptime-wordmark-dark.svg",
    "upgrade-light.svg",
    "upgrade-dark.svg",
    "social-preview.png",
)


def _cmd(command: str) -> str:
    """A command in a box with a copy button."""
    c = html.escape(command)
    return (
        f'<div class="cmd"><code><span class="p">$ </span>{c}</code>'
        f'<button class="copy" type="button" data-copy="{c}">Copy</button></div>'
    )


# Shown on every rename map: the reader came for one migration and is probably in the middle of it.
CTA = (
    '<aside class="cta"><p><b>Updating your own code?</b> DepTime points to each line a dependency update breaks. '
    "Run this after updating your lockfile or requirements, or add the "
    f'<a href="{ACTION}">GitHub Action</a> to check every update pull request. No AI model in the loop.</p>'
    f"{_cmd('uvx deptime check-upgrade')}</aside>"
)


def _newest(dt: DepTime, package: str, prefix: str | None) -> str | None:
    assert dt.store is not None
    versions = [r["version"] for r in dt.store.releases(package)]
    if prefix is not None:
        versions = [v for v in versions if v.startswith(prefix)]
    try:
        return max(versions, key=Version) if versions else None
    except InvalidVersion:
        return versions[-1] if versions else None


def _replacements(dt: DepTime, package: str, event: HistoryEvent) -> list[Replacement]:
    """The replacements the index states at confidence 0.75 or more, as `explanation` would, best first. A
    parameter's come from `symbol_history` as `func(param) -> new`."""
    history = dt.symbol_history(package, event.path)
    param = event.detail.get("param") if event.kind in {"param_removed", "param_deprecated"} else None
    found = []
    for r in history.replacements:
        if r.confidence < EXPLAIN_MIN_CONFIDENCE:
            continue
        if param is None and " -> " not in r.target:
            found.append(r)
        elif param is not None and r.target.startswith(f"{event.path}({param}) -> "):
            found.append(r.model_copy(update={"target": r.target.split(" -> ", 1)[1]}))
    return found


def _artefact(path: str) -> bool:
    """A path no one writes: a module seen through itself (`pydantic.v1.v1.BaseModel`, a re-export loop) or a
    builtin warning a module happens to import (`pydantic.config.DeprecationWarning`). A rename map lists neither.
    Only warnings: methods named `list` or `__init__`, and a library's own `ConnectionError`, are real API."""
    parts = path.split(".")
    builtin = getattr(builtins, parts[-1], None)
    warning = isinstance(builtin, type) and issubclass(builtin, Warning)
    return warning or any(a == b for a, b in pairwise(parts))


def _name(event: HistoryEvent) -> str:
    """The name a row shows: the path, or `path(param)` for a change to one parameter."""
    param = event.detail.get("param") if event.kind.startswith("param_") else None
    return f"{event.path}({param})" if param else event.path


def _back(events: list[tuple[int, HistoryEvent]]) -> set[str]:
    """The names the range has again at its end, after losing them on the way: gradio 6.20's stubs dropped every
    component's event methods (`Button.click`) and 6.27's brought them back. A map lists what the migration takes
    away for good, so these are left off."""
    there: dict[str, bool] = {}
    for _, e in sorted(events, key=lambda pair: pair[0]):  # stable: within a release, `added` comes last
        if e.kind in {"removed", "moved", "param_removed"}:
            there[_name(e)] = False
        elif e.kind in {"added", "param_added"}:
            there[_name(e)] = True
    return {name for name, present in there.items() if present}


def _code(text: str) -> str:
    """A dotted path as code that wraps only after a dot (or an opening parenthesis), never mid-name."""
    return "<code>" + html.escape(text).replace(".", ".<wbr>").replace("(", "<wbr>(") + "</code>"


_TICKED = re.compile(r"`([^`<>]{1,80})`")


def _quote(text: str) -> str:
    """Library text, escaped first, then its `backticked` names shown as code (the escape keeps it data)."""
    return _TICKED.sub(r"<code>\1</code>", html.escape(text))


def _evidence(r: Replacement) -> str:
    text = (r.evidence or "")[:240]
    return (
        f"{html.escape(SOURCES.get(r.source, r.source))}: “{_quote(text)}”" if text else html.escape(r.source)
    )


def _rows(dt: DepTime, rng: Range) -> tuple[str, list[str], list[str]]:
    assert dt.store is not None
    old, new = _newest(dt, rng.package, rng.old), _newest(dt, rng.package, rng.new)
    if old is None or new is None:
        return "", [], []
    lo, _ = dt.store.resolve_version(rng.package, old)
    hi, _ = dt.store.resolve_version(rng.package, new)
    if lo is None or hi is None:
        return "", [], []
    # Every event in the range, not the tool's 500 (`diff_versions`), from the same rows. The additions are read
    # only to tell which removed names came back.
    rows, _ = dt.store.events_between(rng.package, lo["ordinal"], hi["ordinal"], 1_000_000, severity=SEVERITY)
    events = [(r["ordinal"], dt._event(rng.package, r)) for r in rows]
    back = _back(events)
    replaced, gone = [], []
    for _, e in events:
        if e.kind not in CHANGES or _artefact(e.path):
            continue
        name = _name(e)
        if name in back and e.kind in {"removed", "moved", "param_removed"}:
            continue
        found = _replacements(dt, rng.package, e)
        if found:
            use = " or ".join(_code(r.target) for r in found)
            replaced.append(
                f'<tr><td>{_code(name)}</td><td>{use}</td><td class="v">{html.escape(e.version)}</td>'
                f'<td class="ev">{_evidence(found[0])}</td></tr>'
            )
        # Without a replacement to show, only names people write: not `_private` ones or a module's `__all__`.
        elif e.kind == "removed" and e.path.count(".") <= 2 and not e.path.rsplit(".", 1)[-1].startswith("_"):
            gone.append(f'<tr><td>{_code(name)}</td><td class="v">{html.escape(e.version)}</td></tr>')
    return f"{rng.package} {old} → {new}", replaced[:MAX_ROWS], gone[:MAX_ROWS]


def _curated_rows(package: str) -> list[str]:
    return [
        f'<tr><td>{_code(h.path)}</td><td>{_code(h.target)}</td><td class="v"></td>'
        f'<td class="ev">{html.escape(SOURCES["curated"])}: “{_quote(h.quote)}” '
        f'(<a href="{html.escape(h.url)}">source</a>)</td></tr>'
        for h in hints_for(package)
    ]


def _updated(built: str | None) -> str:
    """The index's build date as a reader writes it: "November 18, 2026" (UTC)."""
    try:
        d = datetime.fromisoformat(built or "").astimezone(UTC)
    except ValueError:
        return built or "?"
    return f"{d:%B} {d.day}, {d.year}"


def _wordmark(height: int = 22) -> str:
    return (
        '<picture><source media="(prefers-color-scheme: dark)" srcset="deptime-wordmark-dark.svg">'
        f'<img src="deptime-wordmark-light.svg" alt="DepTime" height="{height}"></picture>'
    )


def _page(
    title: str,
    body: str,
    built: str | None,
    description: str = "",
    canonical: str | None = None,
    image: str | None = None,
) -> str:
    head = ""
    if description:
        d = html.escape(description)
        head += f'<meta name="description" content="{d}"><meta property="og:description" content="{d}">'
    if canonical:
        c = html.escape(canonical)
        head += f'<link rel="canonical" href="{c}"><meta property="og:url" content="{c}">'
    if image:
        head += (
            f'<meta property="og:image" content="{html.escape(image)}"><meta property="og:image:width" '
            'content="2560"><meta property="og:image:height" content="1280">'
            '<meta name="twitter:card" content="summary_large_image">'
        )
    t = html.escape(title)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{t}</title><meta property="og:title" content="{t}"><meta property="og:type" content="website">'
        f'<meta property="og:site_name" content="DepTime">{head}'
        '<link rel="icon" href="deptime-icon.svg" type="image/svg+xml">'
        f'<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">'
        f'<meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)">'
        f"<style>{STYLE}</style></head><body>"
        f'<header class="top"><div class="wrap"><a class="brand" href="./" aria-label="DepTime home">{_wordmark()}</a>'
        f'<nav><a href="./#maps">Rename maps</a><a class="hide-sm" href="{BENCHMARK}">Benchmark</a>'
        f'<a href="{REPO}">GitHub</a></nav></div></header>'
        f"<main>{body}</main>"
        f'<footer class="foot"><div class="wrap"><nav><a href="{REPO}">GitHub</a><a href="{PYPI}">PyPI</a>'
        f'<a href="{ACTION}">GitHub Action</a><a href="{BENCHMARK}">Benchmark</a></nav>'
        "<span>Open source (MIT) · Updated twice a day from each library's releases · Last updated "
        f"{html.escape(_updated(built))}</span></div></footer>"
        f"<script>{SCRIPT}</script></body></html>\n"
    )


def _landing(links: list[tuple[Migration, str, int, int]], packages: int) -> str:
    cards = "".join(
        f'<a class="card" href="{m.slug}.html"><h3>{html.escape(m.title)}</h3>'
        f'<div class="span mono">{html.escape(span)}</div>'
        f'<div class="stats"><span><b>{n}</b> renamed or replaced</span><span><b>{g}</b> removed</span></div></a>'
        for m, span, n, g in links
    )
    features = (
        (
            "line",
            "Every broken line",
            "It finds each use of the updated packages in your code and checks it "
            "against the new release: removed names, keywords no longer accepted, newly required arguments. Each "
            "break comes with its line, the release that changed it, and what to use instead.",
        ),
        (
            "offline",
            "No AI in the loop",
            f"Each release of {packages} fast-moving Python libraries is read "
            "statically, never imported or run, and compared with the one before. Same input, same answer, on your "
            "machine, for free.",
        ),
        (
            "pr",
            "On every update pull request",
            "The GitHub Action checks Dependabot, Renovate and hand-made "
            "updates, marks each broken line on the pull request, and fails the check only when something really "
            "breaks.",
        ),
    )
    cells = "".join(
        f'<div class="card"><div class="ico">{ICONS[icon]}</div><h3>{title}</h3><p>{html.escape(text)}</p></div>'
        for icon, title, text in features
    )
    return (
        '<div class="halo" aria-hidden="true"></div><div class="wrap"><section class="hero"><span class="pill">Open source · Python · Offline</span>'
        "<h1>Know what a Python dependency update breaks before you merge it.</h1>"
        '<p class="lede">DepTime points to each line of your code an update breaks, the release that changed it, '
        "and what to use instead. No AI model in the loop: it runs on your machine, costs nothing, and gives the "
        "same answer every time.</p>"
        f'<div class="actions">{_cmd("uvx deptime check-upgrade")}'
        f'<a class="btn ink" href="{ACTION}">Add the GitHub Action</a><a class="btn" href="{REPO}">View on GitHub</a>'
        "</div></section>"
        '<figure class="demo"><picture><source media="(prefers-color-scheme: dark)" srcset="upgrade-dark.svg">'
        '<img src="upgrade-light.svg" width="760" alt="A terminal: git diff shows two pinned versions updated in '
        "requirements.txt, and deptime check-upgrade lists each line of the project the update breaks, with the "
        'release that changed it and what to use instead."></picture>'
        "<figcaption>A real <code>check-upgrade</code> report from the published index.</figcaption></figure>"
        '<section class="block"><h2>Catch the break in review, not in production</h2>'
        '<p class="sub">Dependency bots tell you an update exists. DepTime tells you what it breaks in your code.</p>'
        f'<div class="grid">{cells}</div></section>'
        '<section class="block"><div class="band"><div><h2>Your coding agent can ask too</h2>'
        "<p>DepTime is an MCP server for Claude Code, Cursor, VS Code, Codex and any client that speaks MCP. "
        "Agents ask what changed in a library, in which release, and what replaces it, before writing code from "
        "memory.</p></div>"
        f"<div>{_cmd('claude mcp add deptime -- uvx deptime mcp')}</div></div></section>"
        '<section class="block" id="maps"><h2>Rename maps</h2>'
        '<p class="sub">What well-known migrations renamed or removed, and what replaces each name, read from the '
        f'libraries\' own code.</p><div class="grid">{cards}</div></section>'
        '<section class="block"><h2>Measured in the open</h2>'
        '<p class="sub">Every test set is collected before DepTime sees it, the pass bar is committed before the '
        "first run, and the misses are published, including how DepTime compares with AI models answering alone. "
        f'<a href="{BENCHMARK}">Read the benchmark →</a></p>'
        f'<div class="tiles">{"".join(f"<div class=tile><b>{n}</b><span>{w}</span></div>" for n, w in PR_RESULT)}'
        f'</div><p class="note">First run on {PR_SET}: <a href="{PR_RESULTS}">results</a>.</p></section></div>'
    )


def build(
    dt: DepTime,
    out: Path,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    base_url: str | None = None,
    assets: Path | None = ASSETS,
) -> list[Path]:
    """Write the pages to `out` and return them; the brand and demo images are copied beside them from `assets`
    (when that folder exists) and aren't in the list."""
    assert dt.store is not None
    out.mkdir(parents=True, exist_ok=True)
    copied = set()
    if assets is not None and assets.is_dir():
        for name in ASSET_FILES:
            if (assets / name).is_file():
                shutil.copyfile(assets / name, out / name)
                copied.add(name)
    built = dt.store.built_at()
    base = base_url.rstrip("/") if base_url else None
    image = f"{base}/social-preview.png" if base and "social-preview.png" in copied else None
    written, links = [], []
    for m in migrations:
        sections, spans = [], []
        replaced: list[str] = []
        gone: list[str] = []
        for rng in m.ranges:
            span, rows, removed = _rows(dt, rng)
            if span:
                spans.append(span)
                replaced += rows
                gone += removed
        if m.curated_from:
            replaced += _curated_rows(m.curated_from)
            spans.append(f"{m.curated_from} → its successor, from the projects' migration guides")
        head = "<thead><tr><th>Old</th><th>Use instead</th><th>Changed in</th><th>Evidence</th></tr></thead>"
        if replaced:
            sections.append(
                f'<section class="changes"><h2>Renamed or replaced<span class="n">{len(replaced)}</span></h2>'
                f'<div class="tw"><table class="replaced">{head}<tbody>{"".join(replaced)}</tbody></table></div>'
                "</section>"
            )
        if gone:
            sections.append(
                f'<section class="changes"><h2>Removed, with no replacement stated<span class="n">{len(gone)}'
                '</span></h2><div class="tw"><table><thead><tr><th>Old</th><th>Gone in</th></tr></thead>'
                f"<tbody>{''.join(gone)}</tbody></table></div></section>"
            )
        span = "; ".join(spans)
        about = []
        if m.ranges:
            about.append(
                "Every name and argument that went away in this migration, read from each release's own code, with "
                "what the library itself, its migration guide, or a move to another path says to use instead."
            )
        if m.curated_from:
            about.append(
                f"The names {html.escape(m.curated_from)}'s migration guide says to replace, with what to use "
                "instead, each quoted from the guide with a link to it."
            )
        intro = (
            f'<nav class="crumbs"><a href="./">DepTime</a><span>/</span><a href="./#maps">Rename maps</a>'
            f"<span>/</span>{html.escape(m.title)}</nav>"
            f'<div class="maphead"><h1>{html.escape(m.title)}<small>What was renamed or removed, and what to use '
            f'instead</small></h1><div class="tag mono">{html.escape(span)}</div>'
            f'<p class="intro">{" ".join(about)}</p></div>'
            '<div class="tiles">'
            + "".join(
                f'<div class="tile"><b>{n}</b><span>{label}</span></div>'
                for n, label in (
                    (len(replaced), "renamed or replaced"),
                    (len(gone), "removed, no replacement stated"),
                )
                if n
            )
            + "</div>"
            f"{CTA}"
            '<div class="toolbar"><input id="filter" class="filter" type="search" placeholder="Filter names, e.g. '
            f'{html.escape(_example(replaced, gone))}" aria-label="Filter names" autocomplete="off">'
            '<span id="count" class="count" aria-live="polite"></span></div>'
        )
        description = (
            f"{m.title}: {len(replaced)} names and arguments renamed or replaced, {len(gone)} removed, and what "
            "to use instead, "
            + ("read from each release's own code." if m.ranges else "from the project's migration guide.")
        )
        page = _page(
            f"{m.title} migration: renamed and removed names",
            f'<div class="wrap">{intro}{"".join(sections)}<p id="none" class="none">No names match.</p></div>',
            built,
            description,
            f"{base}/{m.slug}.html" if base else None,
            image,
        )
        path = out / f"{m.slug}.html"
        path.write_text(page, encoding="utf-8")
        written.append(path)
        links.append((m, span, len(replaced), len(gone)))
    titles = [m.title for m, _, _, _ in links]
    named = ", ".join(titles[:LISTED]) + (f" and {len(titles) - LISTED} more" if len(titles) > LISTED else "")
    (out / "index.html").write_text(
        _page(
            "DepTime: know what a Python dependency update breaks",
            _landing(links, len(dt.coverage.packages)),
            built,
            "Know what a Python dependency update breaks before you merge it: offline, free, no AI in the loop. "
            f"Plus rename maps: {named}.",
            f"{base}/" if base else None,
            image,
        ),
        encoding="utf-8",
    )
    (out / "llms.txt").write_text(
        "# DepTime: Python API rename maps\n\n"
        "> Know what a Python dependency update breaks before you merge it. Each map lists the names and "
        "arguments a migration renamed or removed, and what replaces them, read from the releases' own code.\n\n"
        + "".join(f"- [{m.title}]({m.slug}.html): {n} names replaced, {g} removed\n" for m, _, n, g in links),
        encoding="utf-8",
    )
    written += [out / "index.html", out / "llms.txt"]
    if base:
        urls = "".join(
            f"<url><loc>{html.escape(f'{base}/{p.name}')}</loc></url>" for p in written if p.suffix == ".html"
        )
        (out / "sitemap.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n',
            encoding="utf-8",
        )
        written.append(out / "sitemap.xml")
    return written


def _example(replaced: list[str], gone: list[str]) -> str:
    """A short name from the page for the filter's placeholder: the last public part of the first row's path."""
    for row in replaced + gone:
        name = row.split("<code>", 1)[1].split("</code>", 1)[0].replace("<wbr>", "")
        parts = html.unescape(name).split("(")[0].split(".")
        return next((s for s in reversed(parts) if not s.startswith("_")), parts[-1])
    return "Client"


def main(argv: list[str] | None = None) -> int:
    from deptime.config import Settings, load_coverage
    from deptime.index.location import active_index
    from deptime.index.store import IndexStore

    ap = argparse.ArgumentParser(prog="python -m deptime.site")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=Path("site/dist"))
    b.add_argument("--index", type=Path)
    b.add_argument("--base-url")
    args = ap.parse_args(argv)
    index = args.index or active_index(Settings.from_env().index_path)
    if not index.is_file():
        print(f"no index at {index}: run `deptime index pull` first", file=sys.stderr)
        return 1
    for path in build(DepTime(IndexStore.open(index), load_coverage()), args.out, base_url=args.base_url):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
