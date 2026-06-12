import contextvars
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_STDLIB_ATTRS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
    "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
})

# Pastel-ish 256-color palette tuned for log viewers with a white background
# (Argo CD UI, GitLab Job logs, plain xterm-light). Bright/bold ANSI codes
# wash out on white; these are the dark variants of each hue plus a true
# gray for de-emphasis. Bold is never applied — color carries the signal
# already, doubling it with weight hurts dense lines.
_PALETTE = {
    "red":     "\x1b[38;5;124m",  # dark red — ERROR, cr-name-collision, oom-bump-suppressed, +N% deltas
    "amber":   "\x1b[38;5;130m",  # dark amber — WARNING, oom-bump, freeze, oom-noop, oom
    "green":   "\x1b[38;5;34m",   # mid-dark green — OK, -N% deltas (slightly brighter than 28 for white-bg visibility)
    "blue":    "\x1b[38;5;25m",   # dark blue — config, override, === ns ===, URLs (+ underline)
    "cyan":    "\x1b[38;5;30m",   # dark teal — prometheus, check, mr, →
    "magenta": "\x1b[38;5;90m",   # dark magenta — oom-reset, grow-shrink-clamp
    "gray":    "\x1b[38;5;240m",  # gray — SKIP, DRY RUN, dim labels, info-only lines
    "reset":   "\x1b[0m",
}

# SGR codes for stand-alone attributes composed with foreground colors.
_UNDERLINE_ON  = "\x1b[4m"
_UNDERLINE_OFF = "\x1b[24m"
# Bold reserved exclusively for phase banners — adds weight without adding
# a new hue. Avoided on regular tags/lines because doubling color + weight
# on dense rows reduces scannability; banners are sparse enough to benefit.
_BOLD_ON       = "\x1b[1m"
_BOLD_OFF      = "\x1b[22m"

# Per-tag color mapping. Unknown tags fall through uncolored (better than
# guessing — a new `[tag]` introduced by some future module will still be
# legible). The key is the bare tag (no brackets).
_TAG_COLORS = {
    "OK":                   "green",
    "SKIP":                 "gray",
    "DRY RUN":              "gray",
    "freeze":               "amber",
    "oom":                  "amber",
    "oom-bump":             "amber",
    "oom-noop":             "amber",
    "oom-reset":            "magenta",
    "oom-bump-suppressed":  "red",
    "oom-floor-suppressed": "amber",
    "cr-name-collision":    "red",
    "grow-shrink-clamp":    "magenta",
    "config":               "blue",
    "override":             "blue",
    "git":                  "cyan",
    "prometheus":           "cyan",
    "check":                "cyan",
    "mr":                   "cyan",
    "push":                 "cyan",
}

# Phase tag colors. Phase tags are emitted by the formatter when a
# `phase_ctx(name)` block is active in the call site. Each phase has a
# distinct hue so the operator can tell discovery from recommend from
# result at a glance. Discovery is cyan (not blue) so it doesn't blend
# into the `=== ns ===` separator that's also colored blue inside the
# discovery block. Recommend is magenta (warm, action). Result is green
# (success, pairs with [OK] semantics).
_PHASE_COLORS = {
    "discovery": "cyan",
    "recommend": "magenta",
    "result":    "green",
}

# Status tag at the START of the message (after optional indent), then a
# space. Tag contents are letters, digits, dashes, and a single space (to
# allow `[DRY RUN]`). The trailing whitespace is captured so we can replace
# it with exact-width padding.
_TAG_RE = re.compile(r"^(\s*)(\[[A-Za-z][A-Za-z0-9 \-]*\])(\s+|$)")

# Width that `[tag]` is padded to so the message body starts at a stable
# column. Sized for the longest phase tag (`[recommend]`, 11 chars) so
# `[recommend] [OK] ...` and `[recommend]   sub-line` both land their
# bodies at the same column. Tags longer than this overflow with a single
# trailing space — those are rare and meant to stand out.
_TAG_PAD_WIDTH = 11

# Namespace separator emitted at the start of each ns block in cmd_sync.
# Matched independently from the tag regex so we can color the whole line
# rather than just a prefix.
_NS_RE = re.compile(r"^(\s*)(=== .+ ===)(.*)$")

# Signed-percentage delta (`(+47%)` / `(-12%)`) emitted by writeback's
# `_delta`. Colored intuitively: increases red (cost/risk up, demands a
# look), decreases green (savings, good). Applied AFTER tag handling so
# percentage colors layer on top of whatever tag color the line carries.
# Restricted to a parenthesized signed integer + % to avoid matching the
# unrelated `(workloads=N)` suffix on namespace headers.
_PCT_RE = re.compile(r"\(([+\-])(\d+)%\)")

# URLs in the log: blue + underlined. The URL is the operator-actionable
# bit of the line ("MR opened https://..." / "Pushed: https://..."), so
# it earns the most distinct treatment in the inline palette.
_URL_RE = re.compile(r"https?://\S+")

# Soft de-emphasis for label keywords. Greying out `req=` / `lim=` /
# `containers=` shifts visual weight onto the values themselves so a fast
# scan picks up `cpu:200m` / `mem:512Mi` first. Word-bounded to avoid
# matching the middle of identifiers.
_DIM_KEYWORDS_RE = re.compile(r"\b(req|lim|containers)=")

# Tree connectors (`├─` and `└─`) under `[OK] <ns>:` blocks. Dimming
# them treats the tree as structure (same de-emphasis register as the
# `req=`/`lim=` labels) so the per-container payload reads as the
# primary content.
_TREE_CONNECTOR_RE = re.compile(r"(├─|└─)")

# Branch / file arrow used in `(branch a → b)` and other lineage hints.
# Cyan matches the same hue as `[mr]` since both relate to the MR pipeline.
_ARROW_RE = re.compile(r" → ")

# Lines that are purely informational — emitted by the discovery phase as
# bullet items under each namespace section. Whole-line gray makes the
# block visually settle into a "context, not action" register so the
# operator's attention is reserved for [OK] / [mr] / warnings.
_INFO_LINE_RES = (
    re.compile(r"\bskipping (init )?containers\b"),
)


# --------------------------------------------------------------------------- #
# Phase context                                                                 #
# --------------------------------------------------------------------------- #
# Tracked via a contextvar so the call sites in `main.cmd_sync` can mark
# discovery / recommend / result blocks with a `with` statement instead of
# threading a phase argument through every `_log.info(...)`. The formatter
# reads the contextvar at format-time and prepends a `[<phase>]` tag to
# each non-empty line emitted inside the block.
_phase: contextvars.ContextVar = contextvars.ContextVar("kru_log_phase", default=None)


class phase_ctx:
    """Context manager that tags every log line emitted in its block with
    a `[<phase>]` prefix. Use to separate discovery / recommend / result
    sections in the sync output without touching every call site.

    Nested phases push and restore via `ContextVar.reset` so an outer
    `with phase_ctx("recommend"):` is not clobbered by an inner block.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "phase_ctx":
        self._token = _phase.set(self._name)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._token is not None:
            _phase.reset(self._token)
            self._token = None


def current_phase() -> "str | None":
    """Public accessor for tests / introspection. Returns the active phase
    or `None` outside any `phase_ctx` block.
    """
    return _phase.get()


# --------------------------------------------------------------------------- #
# Per-message colorization                                                      #
# --------------------------------------------------------------------------- #

def _colorize_pcts(msg: str) -> str:
    """Wrap every signed-percentage delta in red (increase) or green
    (decrease). Idempotent on messages with no `(+N%)` / `(-N%)` tokens.
    """
    def repl(m: re.Match) -> str:
        sign, digits = m.group(1), m.group(2)
        color = _PALETTE["red"] if sign == "+" else _PALETTE["green"]
        return f"{color}({sign}{digits}%){_PALETTE['reset']}"
    return _PCT_RE.sub(repl, msg)


def _colorize_inline(msg: str) -> str:
    """Apply token-level coloring to a message body: signed-pct deltas,
    URLs (blue + underline), → arrows (cyan), `req=`/`lim=`/`containers=`
    keyword labels (gray for de-emphasis).

    Run in a specific order so nested escapes don't double-wrap or break:
      1. percentages (longest tokens, least likely to overlap)
      2. URLs (single span, also won't overlap pct or keywords)
      3. arrows (whitespace-bounded ` → `, no overlap)
      4. dim keywords (last so pct and URL colors win on the same line)
    """
    msg = _colorize_pcts(msg)
    msg = _URL_RE.sub(
        lambda m: f"{_PALETTE['blue']}{_UNDERLINE_ON}{m.group(0)}{_UNDERLINE_OFF}{_PALETTE['reset']}",
        msg,
    )
    msg = _ARROW_RE.sub(f" {_PALETTE['cyan']}→{_PALETTE['reset']} ", msg)
    msg = _TREE_CONNECTOR_RE.sub(
        lambda m: f"{_PALETTE['gray']}{m.group(0)}{_PALETTE['reset']}",
        msg,
    )
    msg = _DIM_KEYWORDS_RE.sub(
        lambda m: f"{_PALETTE['gray']}{m.group(0)}{_PALETTE['reset']}",
        msg,
    )
    return msg


def _format_message(msg: str, *, color: bool) -> str:
    """Apply tag-padding + (optionally) ANSI color to a single log message.

    Pure transform: same input always produces the same output; no side
    effects. Used by `_TextFormatter` and exercised directly by the QA so
    regressions in the pad/color logic are caught by `qa_params.py` without
    standing up a stream handler.
    """
    # Namespace header gets colored as a unit (the leading `===` makes the
    # tag regex below not match anyway). Width-pad is not meaningful here.
    m_ns = _NS_RE.match(msg)
    if m_ns:
        indent, header, tail = m_ns.group(1), m_ns.group(2), m_ns.group(3)
        if color:
            return f"{indent}{_PALETTE['blue']}{header}{_PALETTE['reset']}{tail}"
        return msg

    # Whole-line gray for informational bullets (`skipping containers`,
    # `skipping init containers`). Whole-line treatment short-circuits the
    # tag / inline regex chain so the line settles into the "context"
    # register without competing color regions.
    if color:
        for r in _INFO_LINE_RES:
            if r.search(msg):
                return f"{_PALETTE['gray']}{msg}{_PALETTE['reset']}"

    m = _TAG_RE.match(msg)
    if m:
        indent = m.group(1)
        tag_token = m.group(2)
        tag_inner = tag_token[1:-1]
        rest = msg[m.end(2):].lstrip(" ")
        if color:
            color_name = _TAG_COLORS.get(tag_inner)
            tag_str = (
                f"{_PALETTE[color_name]}{tag_token}{_PALETTE['reset']}"
                if color_name else tag_token
            )
            rest = _colorize_inline(rest)
        else:
            tag_str = tag_token
        # Pad to a stable visual width. ANSI escapes have zero printed
        # width so the calculation uses the bare token length.
        pad = max(1, _TAG_PAD_WIDTH - len(tag_token) + 1)
        return f"{indent}{tag_str}{' ' * pad}{rest}"

    # Untagged lines (notably writeback's per-container `cr/c req=... lim=...`
    # delta lines): apply the inline coloring chain so pct / URL / arrow /
    # keyword treatments still happen even without a leading tag.
    if color:
        return _colorize_inline(msg)
    return msg


def log_phase_banner(
    logger: logging.Logger,
    phase: str,
    subtitle: "str | None" = None,
) -> None:
    """Emit a section-header line marking the start of a phase.

    Renders as `─────── PHASE (subtitle) ───────` — heavy box-drawing
    dashes flanking an UPPERCASE bold phase word. The dashes draw a
    clear horizontal divider so the phase transition is visible at a
    glance even in viewers that strip leading whitespace.

      with log.phase_ctx("discovery"):
          log.log_phase_banner(_log, "discovery", subtitle=f"{n_ns} ns, {n_wl} wl")

    `subtitle` is parenthesized inline when provided — use it for
    counts known up-front (discovery: namespace + workload counts;
    result: number of MRs/pushes).

    JSON consumers see `{"message": "...", "phase": "discovery",
    "banner": True, "subtitle": "3 ns, 7 wl"}` and can filter
    `banner=True` to find phase transitions.
    """
    # Text payload assembled here so JSON output sees the same string.
    # Formatter wraps it with bold + color in text mode; JSON strips ANSI
    # (which there are none of in this string anyway).
    word = phase.upper()
    label = f"{word} ({subtitle})" if subtitle else word
    text = f"─────── {label} ───────"
    extras: dict = {"banner": True}
    if subtitle:
        extras["subtitle"] = subtitle
    logger.info("")  # blank line before
    logger.info(text, extra=extras)
    logger.info("")  # blank line after


# Marker the text formatter checks to identify phase-banner lines so it can
# render them as the colored capitalized word (and skip the leading `[tag]`
# regex which would otherwise miss the bare word anyway).
_BANNER_KEY = "banner"

# Marker for per-container delta lines that produced no changes (all four
# delta strings empty). The formatter renders the line whole-gray to make
# the scan jump straight to the colored ones.
_UNCHANGED_KEY = "unchanged"


class _TextFormatter(logging.Formatter):
    """Human-readable formatter. Adds aligned `[tag]` padding for status
    prefixes and (optionally) muted 256-color ANSI for ArgoCD-style log
    viewers with a white background.

    Color is opt-in via the constructor; the caller decides based on
    `log_color` config (`auto` / `always` / `never`). JSON output goes
    through `_JsonFormatter` and never sees these escapes.
    """

    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        raw = record.getMessage()
        # Empty / whitespace-only lines (the explicit `_log.info("")`
        # separators between namespace blocks and around phase banners)
        # keep their visual blankness.
        if not raw.strip():
            out = ""
        elif getattr(record, _BANNER_KEY, False):
            # Phase-banner line — render with bold + phase color across
            # the whole `─── PHASE ───` row. Bold ANSI works in modern
            # log viewers (Argo CD UI uses ansi-up which wraps SGR 1 as
            # font-weight:bold); on the muted 256-color palette tuned
            # for white backgrounds the weight is what carries the
            # "section break" feel.
            phase = _phase.get() or raw.strip().lower()
            if self._color:
                color_name = _PHASE_COLORS.get(phase, "gray")
                out = (
                    f"{_BOLD_ON}{_PALETTE[color_name]}{raw}"
                    f"{_PALETTE['reset']}{_BOLD_OFF}"
                )
            else:
                out = raw
        elif getattr(record, _UNCHANGED_KEY, False) and self._color:
            # Unchanged container lines (all 4 deltas empty) — whole-line
            # gray. Skip the regular tag/inline coloring chain since
            # there's nothing actionable to highlight; the gray-out IS
            # the signal ("scan past me, nothing changed here").
            out = f"{_PALETTE['gray']}{raw}{_PALETTE['reset']}"
        else:
            msg = _format_message(raw, color=self._color)
            level_prefix = self._level_prefix(record.levelno)
            out = f"{level_prefix}{msg}"
        # Standard logging.Formatter appends formatted exception info; the
        # plain override above bypassed that, which made `_log.exception()`
        # silently drop the traceback. Bring it back so failures inside
        # background threads (e.g. the webhook's watch bootstrap) surface
        # in the pod logs instead of needing a debugger attach.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            out += "\n" + record.exc_text
        if record.stack_info:
            out += "\n" + self.formatStack(record.stack_info)
        return out

    def _level_prefix(self, levelno: int) -> str:
        if levelno >= logging.ERROR:
            return (
                f"{_PALETTE['red']}ERROR{_PALETTE['reset']}    "
                if self._color else "ERROR    "
            )
        if levelno >= logging.WARNING:
            return (
                f"{_PALETTE['amber']}WARNING{_PALETTE['reset']}  "
                if self._color else "WARNING  "
            )
        return ""


class _JsonFormatter(logging.Formatter):
    """Structured log formatter for Loki / ELK / Splunk pipelines.

    Beyond the bare `timestamp / level / logger / message` quartet, this
    formatter exposes two computed fields that make per-event filtering
    cheap without needing message-regex parsing on the consumer side:

      - `phase`: the active `phase_ctx` (`discovery` / `recommend` /
        `result`) when the line was emitted. Absent outside any block.
      - `tag`:   the leading bracketed event tag (`OK`, `oom-bump`,
        `freeze`, etc.) without brackets. Absent for untagged lines.
        The text message keeps the bracketed form so consumers that
        want both human-readable and structured views see consistent
        content.

    Any `extra={...}` passed to a log call is copied through unchanged
    (already-working contract — JSON drops keys in `_STDLIB_ATTRS` to
    avoid duplicating bookkeeping fields).
    """

    def format(self, record: logging.LogRecord) -> str:
        raw_msg = _ANSI_RE.sub("", record.getMessage())
        data: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": raw_msg,
        }
        # Extract the leading `[tag]` once, surface as a top-level field
        # so a Loki query like `{tag="oom-bump"}` works directly.
        m = _TAG_RE.match(raw_msg)
        if m:
            data["tag"] = m.group(2)[1:-1]
        phase = _phase.get()
        if phase:
            data["phase"] = phase
        for key, val in record.__dict__.items():
            if key not in _STDLIB_ATTRS:
                data[key] = val
        return json.dumps(data)


def _resolve_color(color_setting: str, fmt_str: str) -> bool:
    """Decide whether to emit ANSI escapes given the operator's color
    preference and the active log format.

    Tri-state:
      - `never`: never color (operator opt-out).
      - `always`: always color, even when piping to a non-tty.
      - `auto` (default): color when the format is `text`. We intentionally
        do NOT gate on `sys.stdout.isatty()` because the dominant deployed
        consumer (Argo CD / GitLab Job logs) reads stdout from a non-tty
        but renders ANSI sequences anyway; gating on isatty would silently
        disable colors in the place we most want them.
    """
    setting = (color_setting or "auto").lower()
    if setting == "never":
        return False
    if setting == "always":
        return True
    # auto
    return fmt_str != "json"


def setup(level: "str | None" = None, fmt: "str | None" = None, color: "str | None" = None) -> None:
    """Configure root logger. Params take precedence over LOG_LEVEL/LOG_FORMAT/LOG_COLOR env vars."""
    level_str = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    fmt_str   = (fmt   or os.environ.get("LOG_FORMAT", "text")).lower()
    color_str = (color or os.environ.get("LOG_COLOR", "auto")).lower()

    level = getattr(logging, level_str, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    if fmt_str == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter(color=_resolve_color(color_str, fmt_str)))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
