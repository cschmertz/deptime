"""Draw the README's demo from a real answer (DT-OSS-02): docs/assets/demo-light.svg and demo-dark.svg.

    uv run python tools/demo_svg.py

Runs `deptime explain` on the README's error against the local index (`deptime index pull` first) and draws the
command and its answer, trimmed to the fields the README shows, as an animated terminal. No value in the picture is
typed by hand: when the answer changes, so do the pictures.

The animation is CSS inside the SVG, which plays where the file is shown as an image (GitHub, PyPI). Every element is
visible unless its animation hides it, so without animation, or with reduced motion, the last frame shows.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"

ERROR = "TypeError: Client.__init__() got an unexpected keyword argument 'proxies'"
INSTALLED = "httpx==0.28.1"
ERROR_FILE = "error.txt"
SHOW = f"cat {ERROR_FILE}"
EXPLAIN = f"deptime explain --installed {INSTALLED} < {ERROR_FILE}"

# The fields of the finding the README's example keeps, in its order.
FIELDS = (
    "symbol",
    "issue",
    "installed_version",
    "last_version_with",
    "first_version_without",
    "deprecated_in",
    "replacements",
    "pin_to_restore",
    "confidence",
)
REPLACEMENT_FIELDS = ("target", "confidence", "source")
# The values that say what changed, in which release, and what to use instead.
EMPHASIZED = {"issue", "last_version_with", "first_version_without", "target", "pin_to_restore"}

WIDTH = 760
PAD_X = 24
BAR = 36  # the window's title bar
FIRST_BASELINE = BAR + 30
LINE = 20
FONT = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

LOOP = 13.0  # seconds
FADE_OUT = (95.0, 97.0)  # percent of the loop: everything fades, and the terminal is empty until it ends
TYPE_SHOW = (0.4, 0.05)  # start (s), seconds per character
ERROR_AT = 1.35
PROMPT_AT = 1.9
TYPE_EXPLAIN = (2.3, 0.04)
ANSWER_AT, ANSWER_STEP = 4.9, 0.09
LINE_FADE = 0.15  # seconds an output line takes to appear


@dataclass(frozen=True)
class Theme:
    name: str
    window: str
    border: str
    bar: str
    text: str
    muted: str
    error: str
    key: str
    emphasis: str


LIGHT = Theme("light", "#F6F8FA", "#D1D9E0", "#EAEEF2", "#1F2328", "#59636E", "#CF222E", "#306998", "#9A6700")
DARK = Theme("dark", "#161B22", "#30363D", "#0D1117", "#E6EDF3", "#9198A1", "#FF7B72", "#739FC4", "#FFD43B")

Span = tuple[str, str]  # (class, text)


def answer() -> dict:
    """The first finding `deptime explain` gives for the README's error."""
    run = subprocess.run(
        ["deptime", "explain", "--installed", INSTALLED],
        input=ERROR + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    findings = json.loads(run.stdout)["findings"]
    if not findings:
        sys.exit("deptime explain returned no finding: is the index pulled (`deptime index pull`)?")
    return findings[0]


def _pair(key: str, value: object) -> list[Span]:
    cls = "e" if key in EMPHASIZED else "t"
    return [("k", json.dumps(key)), ("p", ": "), (cls, json.dumps(value))]


def answer_lines(finding: dict) -> list[list[Span]]:
    """The trimmed finding as pretty-printed JSON, one list of coloured spans per line."""
    lines: list[list[Span]] = [[("p", "{")]]
    for n, field in enumerate(FIELDS):
        comma = [("p", ",")] if n < len(FIELDS) - 1 else []
        if field != "replacements":
            lines.append([("p", "  "), *_pair(field, finding[field]), *comma])
            continue
        lines.append([("p", "  "), ("k", json.dumps(field)), ("p", ": [")])
        replacements = finding[field]
        for m, replacement in enumerate(replacements):
            spans: list[Span] = [("p", "    {")]
            for i, key in enumerate(REPLACEMENT_FIELDS):
                spans += [("p", ", ")] if i else []
                spans += _pair(key, replacement[key])
            spans.append(("p", "}," if m < len(replacements) - 1 else "}"))
            lines.append(spans)
        lines.append([("p", "  ]"), *comma])
    lines.append([("p", "}")])
    return lines


class Reveals:
    """One CSS animation per moment something appears, all on the same loop."""

    def __init__(self) -> None:
        self.rules: list[str] = []

    def at(self, seconds: float, fade: float = 0.0) -> str:
        """The class of an element that appears at `seconds`, over `fade` seconds, and leaves with the rest."""
        name = f"r{len(self.rules)}"
        hidden_until = 100 * seconds / LOOP
        shown_from = hidden_until + max(100 * fade / LOOP, 0.01)
        out_from, out_to = FADE_OUT
        self.rules.append(
            f"@keyframes {name}{{0%,{hidden_until:.2f}%{{opacity:0}}{shown_from:.2f}%,{out_from}%{{opacity:1}}"
            f"{out_to}%,100%{{opacity:0}}}}.{name}{{animation-name:{name}}}"
        )
        return name


def _spans(spans: list[Span]) -> str:
    return "".join(f'<tspan class="{cls}">{escape(text, quote=False)}</tspan>' for cls, text in spans)


def _typed(command: str, start: float, per_char: float, reveals: Reveals) -> tuple[str, float]:
    """`command` as one tspan per character, each appearing in turn, and the time the last one does."""
    out = []
    for i, char in enumerate(command):
        out.append(f'<tspan class="a {reveals.at(start + i * per_char)}">{escape(char, quote=False)}</tspan>')
    return "".join(out), start + len(command) * per_char


def draw(theme: Theme, finding: dict) -> str:
    reveals = Reveals()
    rows: list[str] = []

    def row(body: str, cls: str = "") -> None:
        y = FIRST_BASELINE + LINE * len(rows)
        rows.append(f'<text x="{PAD_X}" y="{y}"{f' class="{cls}"' if cls else ""}>{body}</text>')

    typed, _ = _typed(SHOW, *TYPE_SHOW, reveals)
    row(f'<tspan class="m">$ </tspan>{typed}')
    row(f'<tspan class="x">{escape(ERROR, quote=False)}</tspan>', f"a {reveals.at(ERROR_AT, LINE_FADE)}")
    row("")
    typed, _ = _typed(EXPLAIN, *TYPE_EXPLAIN, reveals)
    row(f'<tspan class="a m {reveals.at(PROMPT_AT)}">$ </tspan>{typed}')
    at = ANSWER_AT
    for spans in answer_lines(finding):
        row(_spans(spans), f"a {reveals.at(at, LINE_FADE)}")
        at += ANSWER_STEP
    cursor_y = FIRST_BASELINE + LINE * len(rows)
    row('<tspan class="m">$ </tspan>', f"a {reveals.at(at + 0.2)}")
    height = cursor_y + 22
    label = (
        f"Terminal demo. The error: {ERROR} The command: {EXPLAIN} DepTime's answer: "
        f"{finding['symbol']} was removed; the last release with it is {finding['last_version_with']}, the first "
        f"without it {finding['first_version_without']}, and it was deprecated in {finding['deprecated_in']}. "
        f"Use {' or '.join(r['target'] for r in finding['replacements'])} instead, or pin "
        f"{finding['pin_to_restore']}."
    )
    # Every `reveals.at` call comes before the stylesheet, which lists their rules.
    cursor = (
        f'<g class="a {reveals.at(at + 0.2)}"><rect class="blink" x="{PAD_X + 17}" y="{cursor_y - 12}" '
        f'width="8" height="15" fill="{theme.muted}"/></g>'
    )
    style = (
        f"text{{font:14px/1 {FONT};fill:{theme.text};white-space:pre}}"
        f".m,.p{{fill:{theme.muted}}}.k{{fill:{theme.key}}}.x{{fill:{theme.error}}}"
        f".e{{fill:{theme.emphasis};font-weight:600}}.bar{{font-size:12px;fill:{theme.muted}}}"
        f".a{{animation-duration:{LOOP}s;animation-iteration-count:infinite;animation-timing-function:linear}}"
        "@keyframes blink{0%{opacity:1}50%,100%{opacity:0}}"
        ".blink{animation:blink 1.1s steps(1,end) infinite}"
        + "".join(reveals.rules)
        + "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
    )
    dots = "".join(
        f'<circle cx="{20 + 18 * i}" cy="{BAR / 2}" r="5" fill="{theme.border}"/>' for i in range(3)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" '
        f'role="img" aria-label="{escape(label)}"><title>{escape(label, quote=False)}</title>'
        f"<style>{style}</style>"
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="10" fill="{theme.window}" '
        f'stroke="{theme.border}"/>'
        f'<path d="M1 {BAR}V10.5A9.5 9.5 0 0 1 10.5 1H{WIDTH - 10.5}A9.5 9.5 0 0 1 {WIDTH - 1} 10.5V{BAR}Z" '
        f'fill="{theme.bar}"/><path d="M1 {BAR}.5H{WIDTH - 1}" stroke="{theme.border}"/>'
        f"{dots}"
        f'<text class="bar" x="{WIDTH / 2}" y="{BAR / 2 + 4}" text-anchor="middle">'
        "a real answer from the published index, trimmed</text>"
        f'<g xml:space="preserve">{"".join(rows)}{cursor}</g></svg>\n'
    )


def main() -> None:
    finding = answer()
    for theme in (LIGHT, DARK):
        path = ASSETS / f"demo-{theme.name}.svg"
        path.write_text(draw(theme, finding), encoding="utf-8")
        print(f"wrote {path.relative_to(ASSETS.parents[1])} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
