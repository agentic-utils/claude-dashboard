#!/usr/bin/env python3
"""Auto-updating terminal dashboard for Claude Code cache-token usage.

Scans the JSONL transcripts under ~/.claude/projects/**/*.jsonl, reads the
per-response `usage` structures, and renders a live truecolour dashboard.
The transcript scan refreshes every 5 minutes (--interval); the screen repaints
~5×/s for the shimmer, live clock, and to surface the background usage fetch.
Covers the last 12 hours in 5-minute buckets.

Three stacked bar charts (24-bit colour, glow gradient, sub-cell-smooth tops,
labelled Y-axis token scale and hourly X-axis):

  1. Input tokens - cache write disposition:
       blue   = uncached            (input_tokens)
       purple = written to 5m cache (ephemeral_5m, == subagent/sidechain work)
       violet = written to 1h cache (ephemeral_1h, == main-thread work)

  2. Context assembly:
       green = pulled from cache (cache_read_input_tokens)
       blue  = new input         (input_tokens + cache_creation, cache-hit turns)
       red   = cache miss         (whole input on turns that read zero from cache)

  3. Output tokens generated (yellow).

Below: a SUMMARY panel and, to its right, an ACTIVE SESSIONS panel listing
sessions active within the lookback window (--active-window, default 1h; also
governs the subagent list in the detail popup) and their main-vs-subagent
fresh-token balance over the last hour and the full window. A renamed session
(/rename) shows its custom title in place of the session id.

Key facts baked in:
  - 5-minute ephemeral cache == subagent/sidechain work; 1-hour cache == main
    thread (verified from the data via isSidechain).
  - Cache miss is inferred: cache_read==0 means the cached prefix was unavailable
    (e.g. expired during an idle gap) so the whole prompt was re-paid. The first
    request of a session also reads 0 - still uncached cost, shown as miss.
  - Each API response spans several JSONL lines sharing one message.id, so
    responses are de-duplicated by message.id.

Press H (or click the History menu tab) for a longer-span HISTORY view:
the same three charts over a configurable window (--history-hours, default 168 =
1 week) with an auto-scaled bucket and a day-by-day axis, a SUMMARY with a $ cost
estimate and cache-hit rate, and click-a-bar drill-down. No active-sessions or
allowance panels there.

Press P (or click the PRs menu tab) for a PRS view: every open PR you authored
plus every branch you've pushed commits to that has no open PR, across every
repo `gh` can see for your account — approval status, CI (click a red dot for
the failing checks), last commit, last comment (click for the full text), and
per-row action buttons (merge, once approved/no-review-needed and CI is green;
draft/ready toggle; close; delete branch), each behind a confirm popup. Needs
the `gh` CLI installed and authenticated (`gh auth login`); the tab degrades to
a message instead of a table if it isn't. Refreshed on --pr-refresh-seconds
(default 300s) — each scan is several `gh` subprocess calls.

Stdlib only (except the optional `gh` CLI for the PRS tab). --once prints a
single frame; --interval overrides the period.

Flags can also be set in .claude-dashboard.rc, next to this script (one flag
per line, '#' comments OK) - CLI flags given at the command line override it.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TRANSCRIPT_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
# Full or partial cwd paths to leave out of every chart/panel entirely - e.g.
# a background/automated job (cron-style `claude -p` run against a fixed
# working directory), not interactive use. No default: empty unless the user
# passes --exclude. Matched case-insensitively as a substring of the cwd (a
# partial path spans multiple path components, e.g. "OneDrive/AI/qmd-memory",
# so this can't be reduced to single-component equality), after normalising
# backslashes to forward slashes - works regardless of which host or path
# style (Windows "C:\...", WSL "/mnt/c/...") wrote the record.
EXCLUDE_PATTERNS = []


def _cwd_excluded(cwd):
    if not cwd or not EXCLUDE_PATTERNS:
        return False
    norm = str(cwd).replace("\\", "/").lower()
    return any(p in norm for p in EXCLUDE_PATTERNS)


WIN_GLOBS_TTL = 60          # seconds between re-checks of logged-in Windows users
_win_roots_cache = {"ts": 0.0, "roots": []}


def is_wsl():
    """True when running under WSL (checked once, cached)."""
    if is_wsl._cached is None:
        try:
            with open("/proc/version", encoding="utf-8") as f:
                is_wsl._cached = "microsoft" in f.read().lower()
        except OSError:
            is_wsl._cached = False
    return is_wsl._cached


is_wsl._cached = None


def _account_uuid(claude_json_path):
    try:
        with open(claude_json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get("oauthAccount") or {}).get("accountUuid")


def _logged_in_windows_users():
    """Windows usernames with an active session, via `query user` through
    cmd.exe (WSL interop). Falls back to every /mnt/c/Users/* profile dir if
    interop is unavailable (e.g. disabled, or query user missing)."""
    try:
        out = subprocess.run(["cmd.exe", "/c", "query user"],
                             capture_output=True, text=True, timeout=5)
        users = []
        for line in out.stdout.splitlines()[1:]:
            line = line.lstrip(">").strip()
            if line:
                users.append(line.split()[0])
        if users:
            return users
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return [os.path.basename(p) for p in glob.glob("/mnt/c/Users/*")
                if os.path.isdir(p)]
    except OSError:
        return []


def windows_transcript_roots():
    """Extra `.claude/projects` roots for Claude Code run on the Windows host
    (e.g. via PowerShell), reached from WSL under /mnt/c. Only included for a
    Windows user that is currently logged in AND signed into the SAME Claude
    account as this WSL session (matched via accountUuid in ~/.claude.json) -
    otherwise an unrelated account's transcripts on a shared machine would
    leak into the dashboard. Cached for WIN_GLOBS_TTL seconds since collect()
    runs on a timer and `query user` is a subprocess spawn (slow: WSL
    interop into a Windows process)."""
    now = time.time()
    if now - _win_roots_cache["ts"] < WIN_GLOBS_TTL:
        return _win_roots_cache["roots"]
    roots = []
    if is_wsl() and os.path.isdir("/mnt/c/Users"):
        own_uuid = _account_uuid(os.path.expanduser("~/.claude.json"))
        if own_uuid:
            for uname in _logged_in_windows_users():
                base = f"/mnt/c/Users/{uname}"
                if _account_uuid(f"{base}/.claude.json") == own_uuid:
                    roots.append(f"{base}/.claude/projects")
    _win_roots_cache["ts"] = now
    _win_roots_cache["roots"] = roots
    return roots
# These window/bucket dimensions are RESOLVED at startup in configure_dimensions()
# from the CLI args and the terminal width; the values here are fallback defaults
# for non-interactive use (import, piped --once when the size is unknown).
WINDOW = timedelta(hours=12)
BUCKET = timedelta(minutes=5)
NUM_BUCKETS = int(WINDOW / BUCKET)          # 144 (width = MARGIN + NUM_BUCKETS)
INTERVAL_SECONDS = int(BUCKET.total_seconds())
# How far back a session (and, in the detail popup, a subagent) counts as
# "active". Default 1h; overridden by --active-window-hours. Set in main().
# Distinct from the fixed "1h main/sub" token column, a fixed 1-hour metric.
ACTIVE_WINDOW = timedelta(hours=1)
CHART_HEIGHT = 8
MIN_BAR_H = 2                               # floor so 3 charts fit ~9 rows (95x9)
MARGIN = 8                                  # left gutter for the Y-axis scale
RIGHT_RESERVE = 1                           # leave the last terminal column unused
TOTAL_WIDTH = MARGIN + NUM_BUCKETS
# When --window-hours is unset the window fills the terminal width and tracks it
# live on resize (re-bucketing on the next collect); a fixed --window-hours does
# not. Set in configure_dimensions().
AUTOFIT = True
MIN_BUCKETS = 20                            # narrowest chart we'll render

# ── history view ──────────────────────────────────────────────────────────────
# A separate, longer-span view (H key / footer) reusing the same chart machinery
# with a coarser bucket. NUM_BUCKETS (the chart width) is shared with the live
# view; the history WINDOW is fixed (--history-hours, default 168 = 1 week) and
# the bucket is derived as window/width, so the span stays exactly a week while
# the bucket scales to the terminal. --history-bucket-minutes overrides the
# bucket instead, deriving the window as bucket*width. Resolved at startup and
# on resize by compute_history_dims().
HISTORY_HOURS = 168.0
HISTORY_BUCKET_MIN = None                   # None => auto-scale; else fixed minutes
HIST_WINDOW = timedelta(hours=HISTORY_HOURS)
HIST_BUCKET = timedelta(minutes=70)
HIST_NUM_BUCKETS = NUM_BUCKETS
# 7×24 grid of effective tokens (local weekday Mon..Sun × hour 0..23) for the
# history activity-heatmap sub-view; populated by collect(track_heatmap=True).
HIST_HEAT = None
# $ cost estimate = effective-tokens × base-input price. Effective tokens are in
# base-input-token-equivalents, so one blended per-MTok input price converts them
# to dollars. Default = Opus 4.8 input ($5/MTok); --price-per-mtok overrides.
PRICE_PER_MTOK = 5.0
# Active view dimensions, set per render by render_frame from its `mode` arg. In
# live mode they mirror WINDOW/BUCKET; in history mode they hold HIST_WINDOW/
# HIST_BUCKET so the chart axis, bucket-popup span, and summary label all read
# the right window without threading params through the whole render stack.
VIEW_WINDOW = WINDOW
VIEW_BUCKET = BUCKET
VIEW_DAILY = False                          # day-boundary X-axis (history) vs hourly

# ── 24-bit truecolour palette ────────────────────────────────────────────────
CO = {
    "uncached": (84, 160, 255),     # blue
    "c5m":      (170, 120, 255),    # purple  (subagent)
    "c1h":      (214, 150, 255),    # violet  (main)
    "read":     (52, 224, 150),     # green
    "new":      (84, 160, 255),     # blue
    "miss":     (255, 88, 96),      # red
    "output":   (255, 205, 82),     # yellow
    "main":     (84, 160, 255),     # blue
    "sub":      (170, 120, 255),    # purple
}
ACCENT = (90, 232, 232)             # cyan
ACCENT2 = (170, 120, 255)           # purple
TEXT = (216, 220, 240)
DIM = (124, 128, 158)
DIM2 = (72, 74, 102)

PARTIAL = " ▁▂▃▄▅▆▇█"               # 0..8 sub-cell fill levels
CHIP = "▆"

TICK_SECONDS = 0.2                  # repaint cadence (shimmer animation @5fps)
PROGRESS_INTERVAL = 0.15            # collect()'s in-scan progress_cb cadence
USAGE_REFRESH = 300                 # seconds between live-usage refetches
USAGE_BACKOFF = 900                 # after a 429, wait this long before retrying
LOGIN_INLINE_TIMEOUT = 45           # give inline (no-suspend) login this long before
                                     # falling back to a real tty (SSO flows that need
                                     # keyboard input would otherwise hang forever silently)

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "claude-dashboard.log")
RC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".claude-dashboard.rc")
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ccmon")


def rgb(c, text, bold=False):
    r, g, b = c
    b0 = "\033[1m" if bold else ""
    return f"{b0}\033[38;2;{r};{g};{b}m{text}\033[0m"


def styled(text, fg, bg=None, bold=False, underline=False):
    """rgb() plus optional background and SGR-4 underline — the underline is for
    Win3.1-style menu accelerator letters."""
    if text == "":
        return ""
    codes = []
    if bold:
        codes.append("1")
    if underline:
        codes.append("4")
    codes.append(f"38;2;{fg[0]};{fg[1]};{fg[2]}")
    if bg is not None:
        codes.append(f"48;2;{bg[0]};{bg[1]};{bg[2]}")
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def shade(c, f):
    return (int(c[0] * f), int(c[1] * f), int(c[2] * f))


def lerp(a, b, f):
    return a + (b - a) * f


def grad_rule(width, c1, c2, char="━"):
    if width <= 1:
        return rgb(c1, char * max(width, 0))
    return "".join(
        rgb((int(lerp(c1[0], c2[0], i / (width - 1))),
             int(lerp(c1[1], c2[1], i / (width - 1))),
             int(lerp(c1[2], c2[2], i / (width - 1)))), char)
        for i in range(width)
    )


def parse_ts(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Force tz-aware: a naive timestamp (no offset, no Z) would otherwise raise
    # TypeError when compared against the aware `cutoff` and crash collect().
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def empty_bucket() -> dict:
    return {
        "uncached": 0, "c5m": 0, "c1h": 0,   # chart 1 (write disposition)
        "read": 0, "new": 0, "miss": 0,      # chart 2 (assembly)
        "output": 0, "responses": 0,
    }


def add_usage(bk, inp, f5, f1, read, fresh, out):
    """Accumulate one response's usage into a bucket. Shared by the global
    buckets and per-session buckets so the two never drift."""
    bk["uncached"] += inp
    bk["c5m"] += f5
    bk["c1h"] += f1
    bk["read"] += read
    if read > 0:
        bk["new"] += fresh
    else:
        bk["miss"] += fresh
    bk["output"] += out
    bk["responses"] += 1


def eff_tokens(uncached, c5m, c1h, read, output):
    """Effective tokens: everything normalised to base-input-token-equivalents
    using Anthropic's per-token price multipliers. 5m cache write = 1.25x base
    input, 1h write = 2x, cache read = 0.1x, uncached input = 1x, and OUTPUT =
    5x base input (the output:input price ratio, uniform across Claude models).
    One definition shared by the bucket summary and the per-session accounting
    so the two never drift."""
    return uncached + 1.25 * c5m + 2 * c1h + 0.1 * read + 5 * output


def model_max_window(model):
    # Max context a model CAN do. FINDING (2026-06): the 1M context is a per-
    # request beta header, NOT a model property — it's stripped from the logged
    # model id, absent from every usage/beta field, and not queryable via any
    # API after the fact. So we grade against the model's *capability*: any
    # Opus or Sonnet generation supports the 1M beta -> grade at 1M (a real 1M
    # session then never false-flashes at 175k); Haiku caps at 200k. Trade-off:
    # an Opus/Sonnet run in plain 200k mode under-warns (won't alarm near its
    # 200k wall) — acceptable, since the 1M beta is opt-in and the alarm is for
    # big contexts.
    if not model:
        return 200_000
    m = model.lower()
    if "haiku" in m:
        return 200_000
    if "opus" in m or "sonnet" in m:
        return 1_000_000
    return 200_000


def window_for(model, peak):
    # 1M if the model can do it, OR if we've provably seen this thread exceed
    # 200k (which can only happen in a 1M context); else the model's max.
    if (peak or 0) > 200_000:
        return 1_000_000
    return model_max_window(model)


def session_window(s):
    return window_for(s.get("model"), s.get("peak_main", 0))


def sub_window(s):
    return window_for(s.get("peak_sub_model"), s.get("peak_sub", 0))


def ctx_grade(size, window):
    """Return (colour, flashing) for a context size. Five tiers — green, yellow,
    amber, red, flashing red — with thresholds scaled to the window. Bands:
      200k window:  ≤100k g · ≤125k y · ≤150k a · ≤175k r · >175k flashing
      1M  window:   ≤150k g · ≤300k y · ≤450k a · ≤600k r · >600k flashing"""
    if window >= 1_000_000:
        g, y, a, r = 150_000, 300_000, 450_000, 600_000
    else:
        g, y, a, r = 100_000, 125_000, 150_000, 175_000
    if size > r:
        return HOT_C, True          # flashing red
    if size > a:
        return HOT_C, False         # red
    if size > y:
        return ORANGE_C, False      # amber
    if size > g:
        return WARN_C, False        # yellow
    return OK_C, False              # green


def ctx_dot(size, window, now):
    """The traffic-light ● for a context size. Flashes (2s period, 1s on / 1s
    off) when in the flashing-red band; `now` drives the blink."""
    col, flashing = ctx_grade(size, window)
    if flashing and int(now.timestamp()) % 2:      # off half of the 2s period
        return rgb(shade(HOT_C, 0.22), "●")
    return rgb(col, "●")


def _clean(s):
    """Strip control bytes (incl. ESC/CSI) from any transcript-derived string
    before it is painted to the terminal. Slugs, project names, model ids and
    API error text come from `~/.claude/projects/**` — untrusted input — and are
    rendered via rgb()/_padcol, which only PREPEND colour codes. Without this a
    transcript carrying raw escape sequences could drive the cursor, set the
    title, or write the clipboard (OSC-52), and also corrupts _visible_len/_padcol
    alignment. Strips C0, DEL, and C1 (0x80-0x9f, which includes 8-bit CSI)."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", s) if isinstance(s, str) else s


def _err_text(rec):
    m = rec.get("message") or {}
    c = m.get("content")
    if isinstance(c, list):
        t = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if t.strip():
            return _clean(t.strip())
    if isinstance(c, str) and c.strip():
        return _clean(c.strip())
    e = rec.get("error")
    return _clean(e) if isinstance(e, str) else ""


def model_color(name):
    """A stable colour for a model id (the history model-mix chart), matched by
    family substring. Unknown models fall back to neutral grey."""
    m = (name or "").lower()
    if "opus" in m:
        return (84, 160, 255)       # blue
    if "sonnet" in m:
        return (52, 224, 150)       # green
    if "haiku" in m:
        return (255, 138, 56)       # amber
    if "fable" in m or "mythos" in m:
        return (170, 120, 255)      # purple
    return (150, 150, 170)          # grey / unknown


def short_model(m):
    """Compact model id for display: 'claude-opus-4-8' -> 'opus-4-8'."""
    if not m or m == "<synthetic>":
        return "?"
    return _clean(m[7:] if m.startswith("claude-") else m)


def new_session(sid, ts, rec, num_buckets=None):
    """Factory for a per-session stats dict. `last` means the last SUCCESSFUL
    turn ts (None until a usage record is seen); `last_act` is the last ANY
    activity ts (usage or surfaced error). `num_buckets` sizes the per-session
    bucket array; defaults to the global (live) NUM_BUCKETS."""
    nb = NUM_BUCKETS if num_buckets is None else num_buckets
    return {
        "sid": sid, "name": None, "last": None, "cwd": rec.get("cwd") or "",
        "main_12": 0, "sub_12": 0, "main_1h": 0, "sub_1h": 0,
        "ctx": 0, "ctx_ts": None, "model": None,
        "peak_main": 0, "peak_sub": 0, "peak_sub_model": None,
        "eff_main_1h": 0.0, "eff_main_12": 0.0,
        "eff_sub_1h": 0.0, "eff_sub_12": 0.0,
        "subs": {},   # agentId -> per-subagent detail
        "buckets": [empty_bucket() for _ in range(nb)],
        "err": None, "last_act": ts,
    }


def _slugged_patterns():
    """EXCLUDE_PATTERNS, with path separators folded to '-' to match a
    project's on-disk directory name - which is a slug of its cwd with every
    '/' or '\\' replaced by '-' (e.g. cwd "C:\\Users\\doug\\OneDrive\\AI\\
    qmd-memory" -> dir "C--Users-doug-OneDrive-AI-qmd-memory"), so a
    multi-component pattern like "OneDrive/AI/qmd-memory" has to be folded
    the same way before it can be matched against that slug."""
    return [p.replace("\\", "-").replace("/", "-") for p in EXCLUDE_PATTERNS]


def _walk_jsonl_paths(root):
    """Yield every *.jsonl under `root`, pruning whole subtrees whose on-disk
    project directory name (a slug of its cwd) contains an excluded pattern.
    Pruning during the walk (rather than globbing everything and filtering
    paths after) avoids listing/stat-ing an excluded tree at all - the
    dominant cost on a slow filesystem (e.g. WSL's /mnt/c DrvFs mount) when
    that tree is large, as e.g. the QMD dream job's chunk/subagent files are."""
    if not os.path.isdir(root):
        return
    patterns = _slugged_patterns()
    for dirpath, dirnames, filenames in os.walk(root):
        if patterns:
            dirnames[:] = [d for d in dirnames
                           if not any(p in d.lower() for p in patterns)]
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def _all_transcript_paths():
    yield from _walk_jsonl_paths(os.path.expanduser("~/.claude/projects"))
    for root in windows_transcript_roots():
        yield from _walk_jsonl_paths(root)


def collect(now: datetime, window=None, bucket=None, num_buckets=None,
            track_models=False, track_heatmap=False, progress_cb=None,
            seed_sessions=None):
    """Return (buckets, sessions): time buckets oldest->newest plus per-session
    cache stats, all from de-duplicated usage records. `window`/`bucket`/
    `num_buckets` default to the live globals; the history view passes its own
    (longer) span and coarser bucket so the same scan feeds both views.
    `track_models` adds a per-bucket {model: effective-tokens} map under the
    extra "models" key (ignored by the fixed-key aggregation loops) for the
    history model-mix chart.

    `seed_sessions`, if given, is the caller's PREVIOUS `sessions` dict,
    mutated and returned in place instead of starting from empty. A session
    already in it stays visible (with its stale, previous-scan numbers)
    until this scan re-touches it — the whole point being a refresh never
    drops the visible list back to nothing while it re-populates. A session
    is re-touched by discarding its carried-over entry and rebuilding it
    fresh (via new_session) on FIRST touch this call, so its numbers are a
    clean re-aggregation, not stale-plus-incremental double counting;
    further touches this call accumulate into that fresh entry as normal.
    At the end, any carried-over session this call never touched is dropped
    if it's aged out of `window` — mtime_floor already means "never touched"
    only happens when a session's file was entirely skipped as older than
    the window, so this is nearly always true; the last_act check is the
    correctness guard for the rare other case.

    `progress_cb(buckets, sessions)`, if given, is called once immediately
    (before any file is read - so a caller can paint a first frame right
    away) and then again every PROGRESS_INTERVAL seconds while the scan is
    still running, with the in-progress `buckets`/`sessions` - the same
    objects this call will go on to return, mutated in place, so a slow scan
    (many transcripts, or a slow filesystem) shows sessions appearing
    incrementally instead of one long blank wait. Single-threaded: the
    callback runs on this thread between files, never concurrently with the
    mutation, so there's no partial-write tearing to guard against."""
    window = WINDOW if window is None else window
    bucket = BUCKET if bucket is None else bucket
    num_buckets = NUM_BUCKETS if num_buckets is None else num_buckets
    cutoff = now - window
    last_hour = now - timedelta(hours=1)
    mtime_floor = cutoff.timestamp() - 1
    buckets = [empty_bucket() for _ in range(num_buckets)]
    if track_models:
        for b in buckets:
            b["models"] = {}        # model id -> effective tokens (extra key)
    heat = [[0.0] * 24 for _ in range(7)] if track_heatmap else None
    sessions: dict[str, dict] = {} if seed_sessions is None else seed_sessions
    touched_sids: set[str] = set()   # sids (re)touched THIS call
    seen: set[str] = set()
    titles: dict[str, str] = {}   # sid -> custom session title (latest /rename)

    def touch(sid, ts, rec):
        """Session dict for `sid`, rebuilt fresh on this call's first touch
        (discarding any carried-over seed entry) so a re-scan's numbers are
        never stale-plus-incremental double counted; later touches this call
        reuse that fresh entry."""
        if sid not in touched_sids:
            sessions[sid] = new_session(sid, ts, rec, num_buckets)
            touched_sids.add(sid)
        return sessions[sid]

    if progress_cb is not None:
        progress_cb(buckets, sessions)
    last_progress = time.monotonic()

    for path in _all_transcript_paths():
        if progress_cb is not None:
            t = time.monotonic()
            if t - last_progress >= PROGRESS_INTERVAL:
                progress_cb(buckets, sessions)
                last_progress = t
        try:
            if os.path.getmtime(path) < mtime_floor:
                continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # A /rename writes a standalone metadata record with no
                    # timestamp/usage; it'd be dropped by the cutoff check below.
                    # Capture the latest title per session (file order is append
                    # order, so last wins) and attach it after the scan.
                    if rec.get("type") == "custom-title":
                        t = _clean(rec.get("customTitle") or "").strip()
                        if t:
                            tsid = rec.get("sessionId") or os.path.basename(path)[:-6]
                            titles[tsid] = t
                        continue

                    msg = rec.get("message") or {}
                    ts = parse_ts(rec.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    sid = rec.get("sessionId") or os.path.basename(path)[:-6]
                    if _cwd_excluded(rec.get("cwd")):
                        continue

                    # Surfaced API failures (synthetic assistant records) carry
                    # no usage, so they'd be skipped by the usage check below.
                    # Handle them first: record the latest error per session.
                    if msg.get("isApiErrorMessage"):
                        s = touch(sid, ts, rec)
                        status = rec.get("apiErrorStatus")
                        text = _err_text(rec)
                        if s["err"] is None or ts >= s["err"]["ts"]:
                            s["err"] = {"ts": ts, "status": status, "text": text}
                        if rec.get("cwd"):
                            s["cwd"] = rec["cwd"]
                        if s["last_act"] is None or ts > s["last_act"]:
                            s["last_act"] = ts
                        continue

                    usage = msg.get("usage")
                    if not usage:
                        continue
                    key = msg.get("id") or rec.get("requestId")
                    if key is not None:
                        if key in seen:
                            continue
                        seen.add(key)

                    idx = int((ts - cutoff) / bucket)
                    idx = min(max(idx, 0), num_buckets - 1)
                    b = buckets[idx]

                    cc = usage.get("cache_creation") or {}
                    inp = usage.get("input_tokens", 0) or 0
                    creation = usage.get("cache_creation_input_tokens", 0) or 0
                    read = usage.get("cache_read_input_tokens", 0) or 0
                    f5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
                    f1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
                    fresh = inp + creation
                    out = usage.get("output_tokens", 0) or 0
                    total_in = inp + creation + read
                    eff = eff_tokens(inp, f5, f1, read, out)
                    model = msg.get("model")

                    # Charts 1 & 2 + output/responses for the global bucket.
                    add_usage(b, inp, f5, f1, read, fresh, out)
                    if track_models:        # history model-mix: eff tokens by model
                        mk = short_model(model)
                        b["models"][mk] = b["models"].get(mk, 0) + eff
                    if heat is not None:    # activity heatmap: eff by weekday×hour
                        loc = ts.astimezone()
                        heat[loc.weekday()][loc.hour] += eff

                    # Per-session drill-down: split fresh tokens (new work) by
                    # main thread vs subagent (sidechain). Fresh, not total
                    # input, so the main thread's huge cheap cache reads don't
                    # drown the subagent signal.
                    side = "sub" if rec.get("isSidechain") else "main"
                    s = touch(sid, ts, rec)
                    # `last` = last SUCCESSFUL turn (the "last prompt" baseline and
                    # the success cutoff for errored_last); `last_act` = any activity.
                    if s["last"] is None or ts > s["last"]:
                        s["last"] = ts
                        if rec.get("cwd"):
                            s["cwd"] = rec["cwd"]
                    if s["last_act"] is None or ts > s["last_act"]:
                        s["last_act"] = ts
                    s[f"{side}_12"] += fresh
                    if ts >= last_hour:
                        s[f"{side}_1h"] += fresh

                    # Effective-token accounting (real cache-pricing multipliers,
                    # in token-equivalents), split main vs subagent.
                    s[f"eff_{side}_12"] += eff
                    if ts >= last_hour:
                        s[f"eff_{side}_1h"] += eff

                    # Per-subagent detail, keyed by the stable agentId per run.
                    if side == "sub":
                        aid = rec.get("agentId") or "untagged"
                        sub = s["subs"].get(aid)
                        if sub is None:
                            # The transcript `slug` is per-session, not
                            # per-subagent — every subagent in one session
                            # shares it (e.g. "shimmering-dancing-rainbow"),
                            # so in this single-session popup it just repeats.
                            # Show the agentId instead; it is genuinely unique.
                            sub = s["subs"][aid] = {
                                "slug": aid,
                                "start": ts, "stop": ts, "peak": 0, "eff": 0.0,
                                "model": model}
                        sub["start"] = min(sub["start"], ts)
                        sub["stop"] = max(sub["stop"], ts)
                        sub["peak"] = max(sub["peak"], total_in)
                        sub["eff"] += eff
                        if model:
                            sub["model"] = model

                    # Context size = latest MAIN-thread turn's total input;
                    # peak_main = deepest ever, used to infer the 1M window.
                    if side == "main":
                        if total_in > s["peak_main"]:
                            s["peak_main"] = total_in
                        if s["ctx_ts"] is None or ts > s["ctx_ts"]:
                            s["ctx_ts"] = ts
                            s["ctx"] = total_in
                            if model:
                                s["model"] = model

                    # Mirror peak_main for subagents to infer their window.
                    if side == "sub" and total_in > s["peak_sub"]:
                        s["peak_sub"] = total_in
                        s["peak_sub_model"] = model

                    # Per-session buckets feed the click-through popup charts.
                    add_usage(s["buckets"][idx], inp, f5, f1, read, fresh, out)
        except OSError:
            continue

    # A carried-over (seed_sessions) session this call never touched has aged
    # out of `window` — drop it. (In practice this is the ONLY way a session
    # goes untouched: its file's mtime already failed mtime_floor above, since
    # any activity within window would have updated the file's mtime too.
    # The last_act check is belt-and-braces, not the primary mechanism.)
    for sid in list(sessions):
        if sid not in touched_sids:
            s = sessions[sid]
            if s.get("last_act") is None or s["last_act"] < cutoff:
                del sessions[sid]

    # Attach custom /rename titles to their sessions (titles may be seen before
    # the session has any usage record, so this is done after the full scan).
    for tsid, t in titles.items():
        s = sessions.get(tsid)
        if s is not None:
            s["name"] = t

    if track_heatmap:
        global HIST_HEAT
        HIST_HEAT = heat
    return buckets, sessions


# ── helpers ──────────────────────────────────────────────────────────────────

def fmt(n):
    return f"{n:,}"


def fmt_compact(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def fmt_window(td):
    """A timedelta as a compact label for the UI: '1h', '30m', '1h30m'."""
    m = int(td.total_seconds() // 60)
    if m % 60 == 0:
        return f"{m // 60}h"
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"


def pct(part, whole):
    return f"{(100.0 * part / whole):.1f}%" if whole else "n/a"


def _visible_len(s):
    """Length of a string ignoring ANSI SGR sequences."""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            out += 1
            i += 1
    return out


def _padcol(s, width):
    return s + " " * max(width - _visible_len(s), 0)


def _clip(s, width):
    """Truncate a (possibly ANSI-styled) string to `width` visible chars,
    keeping SGR codes intact and resetting at the end."""
    if _visible_len(s) <= width:
        return s
    out, vis, i = [], 0, 0
    while i < len(s) and vis < width:
        if s[i] == "\033":
            j = i
            while j < len(s) and s[j] != "m":
                j += 1
            out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(s[i])
            vis += 1
            i += 1
    out.append("\033[0m")
    return "".join(out)


def _slice_from(s, start):
    """Visible chars of a (possibly ANSI-styled) string from column `start`
    onward: skips `start` visible chars, then re-emits the last SGR code seen
    so the remainder keeps its color instead of falling back to the
    terminal's default. Pairs with _clip (the [0, start) prefix) to carve a
    middle span — e.g. a modal overlay's rectangle — out of a line so a
    repaint can skip it without touching those columns at all."""
    if start <= 0:
        return s
    vis, i, active = 0, 0, ""
    while i < len(s) and vis < start:
        if s[i] == "\033":
            j = i
            while j < len(s) and s[j] != "m":
                j += 1
            active = s[i:j + 1]
            i = j + 1
        else:
            vis += 1
            i += 1
    return active + s[i:]


def fit_overlay(lines, cols, rows, scroll):
    """Fit a bordered modal into the terminal. Clips every line to the width;
    when the modal is taller than the screen, pins the top and bottom border
    rows and scrolls the middle, drawing a vertical scrollbar in the last inner
    column. Returns (visible_lines, max_scroll)."""
    maxw = min(max((_visible_len(l) for l in lines), default=0), cols)
    if len(lines) <= rows:                       # fits whole: just clip width
        return [_clip(ln, maxw) for ln in lines], 0

    top, mid, bot = lines[0], lines[1:-1], lines[-1]
    view_h = max(rows - 2, 1)                     # rows for the scrolling middle
    max_scroll = max(0, len(mid) - view_h)
    scroll = max(0, min(scroll, max_scroll))
    window = mid[scroll:scroll + view_h]
    inner_w = maxw - 1                            # last col is the scrollbar
    thumb = max(1, round(view_h * view_h / len(mid)))
    pos = round(scroll * (view_h - thumb) / max_scroll) if max_scroll else 0

    out = [_clip(top, maxw)]
    for i, ln in enumerate(window):
        on = pos <= i < pos + thumb
        out.append(_padcol(_clip(ln, inner_w), inner_w)
                   + rgb(ACCENT if on else DIM2, "█" if on else "░"))
    out.append(_clip(bot, maxw))
    return out, max_scroll


# ── charts ───────────────────────────────────────────────────────────────────

def build_column(vc, total, maxt, height):
    """Return `height` cells bottom->top as (rgb|None, char). Sub-cell smooth:
    8 sub-levels per cell, so bar tops render as partial blocks."""
    col = [(None, " ")] * height
    if total <= 0 or maxt <= 0:
        return col
    units = height * 8
    sub = min(max(int(round(total / maxt * units)), 1), units)
    nz = [i for i, (_, v) in enumerate(vc) if v > 0]
    alloc = [0] * len(vc)
    if sub >= len(nz):
        # Seed each active segment one sub-cell so it never vanishes, then
        # share the rest by largest remainder.
        for i in nz:
            alloc[i] = 1
        fr = []
        for i in nz:
            e = vc[i][1] / total * sub
            alloc[i] += max(int(e) - 1, 0)
            fr.append((e - int(e), i))
        used = sum(alloc)
        for _, i in sorted(fr, reverse=True)[:max(sub - used, 0)]:
            alloc[i] += 1
    else:
        for i in sorted(nz, key=lambda i: vc[i][1], reverse=True)[:sub]:
            alloc[i] = 1

    contrib = [dict() for _ in range(height)]
    filled = [0] * height
    pos = 0
    for (color, _), n in zip(vc, alloc):
        for _ in range(n):
            ci = pos // 8
            if ci < height:
                contrib[ci][color] = contrib[ci].get(color, 0) + 1
                filled[ci] += 1
            pos += 1
    for ci in range(height):
        if filled[ci] <= 0:
            continue
        color = max(contrib[ci].items(), key=lambda kv: kv[1])[0]
        col[ci] = (color, PARTIAL[min(filled[ci], 8)])
    return col


def render_chart(title, keys, buckets, height, now, anim=0,
                 short_title=None, legend_items=None, compact=False, axes=True,
                 series_of=None, legend_str=None):
    """Render one bar chart as a list of lines. Compact mode folds the title and
    legend onto a single header line (using `short_title`) and drops the hourly
    tick row, saving 2 rows. axes=False also drops the baseline rule. Non-compact
    behaviour (title line only; legend drawn externally) is unchanged.

    Normally each bar is stacked from `keys` against the fixed CO palette. Pass
    `series_of(b) -> [(rgb_tuple, value), ...]` (with a pre-built `legend_str`)
    to stack arbitrary, already-coloured series instead — used by the history
    model-mix chart, whose series (one per model) aren't in CO."""
    if series_of is not None:
        series = [series_of(b) for b in buckets]
        totals = [sum(v for _, v in s) for s in series]
        maxt = max(totals) if totals else 0
        columns = [build_column(s, tot, maxt, height)
                   for s, tot in zip(series, totals)]
    else:
        totals = [sum(b[k] for k in keys) for b in buckets]
        maxt = max(totals) if totals else 0
        columns = [build_column([(CO[k], b[k]) for k in keys], tot, maxt, height)
                   for b, tot in zip(buckets, totals)]

    if compact:
        head = ("  " + rgb(ACCENT, "▸ ", bold=True)
                + rgb(TEXT, short_title or title, bold=True))
        if legend_str is not None:
            head += "   " + legend_str
        elif legend_items:
            head += "   " + legend(legend_items)
        lines = [head]
    else:
        lines = ["  " + rgb(ACCENT, "▸ ", bold=True) + rgb(TEXT, title, bold=True)]
    for row in range(height - 1, -1, -1):
        f = 0.5 + 0.5 * (row / (height - 1)) if height > 1 else 1.0
        if row % 2 == 1:                       # Y-axis scale, every other cell
            val = maxt * (row + 1) / height
            label = rgb(DIM, fmt_compact(round(val)).rjust(MARGIN - 2)) + "  "
        else:
            label = " " * MARGIN
        cells = []
        for i, (base, ch) in enumerate(col[row] for col in columns):
            if base:
                wave = 1.0 + 0.18 * math.sin(0.20 * i + 0.45 * row - 0.11 * anim)
                ff = max(0.12, min(1.0, f * wave))
                cells.append(rgb(shade(base, ff), ch))
            else:
                cells.append(" ")
        body = "".join(cells)
        lines.append(label + body)

    if not axes:                 # tightest tier: bars only, no baseline/ticks
        return lines
    # X-axis baseline + tick labels. Live (VIEW_DAILY off): hourly "H:00".
    # History (VIEW_DAILY on): one label per local-midnight day boundary ("Mon26")
    # so a week of bars stays readable. Dimensions come from the active view
    # (VIEW_WINDOW/VIEW_BUCKET) and the rendered bucket count.
    nb = len(buckets)
    axis = [" "] * nb
    local_cut = (now - VIEW_WINDOW).astimezone()
    local_now = now.astimezone()
    span = VIEW_BUCKET.total_seconds()
    if VIEW_DAILY:
        tick = local_cut.replace(hour=0, minute=0, second=0, microsecond=0)
        if tick < local_cut:
            tick += timedelta(days=1)
        step = timedelta(days=1)

        def fmt_lab(d):
            return f"{d:%a%d}"
    else:
        tick = local_cut.replace(minute=0, second=0, microsecond=0)
        if tick < local_cut:
            tick += timedelta(hours=1)
        step = timedelta(hours=1)

        def fmt_lab(d):
            return f"{d.hour}:00"
    while tick <= local_now:
        pos = round((tick - local_cut).total_seconds() / span)
        lab = fmt_lab(tick)
        start = min(pos, nb - len(lab))
        for i, ch in enumerate(lab):
            if 0 <= start + i < nb:
                axis[start + i] = ch
        tick += step
    lines.append(rgb(DIM, "0".rjust(MARGIN - 1)) + " "
                 + rgb(DIM2, "└" + "─" * (nb - 1)))
    if not compact:              # compact folds labels into the header instead
        lines.append(" " * MARGIN + rgb(DIM, "".join(axis)))
    return lines


def legend(items):
    return "   ".join(rgb(CO[k], CHIP) + " " + rgb(DIM, label) for k, label in items)


# ── panels ───────────────────────────────────────────────────────────────────

def panel(title, rows, inner, title_len=None):
    """`title` is plain text (coloured here) unless `title_len` is given, in
    which case `title` is taken as already-styled and `title_len` is its visible
    width — used by the SUMMARY panel to draw multi-segment clickable tabs."""
    if title_len is None:
        head, tl = rgb(ACCENT, title, bold=True), _visible_len(title)
    else:
        head, tl = title, title_len
    fill = max(inner - 3 - tl, 0)
    out = [rgb(DIM2, "╭─ ") + _clip(head, inner - 2) + rgb(DIM2, " " + "─" * fill + "╮")]
    for r in rows:
        # Clip as well as pad: a content row wider than `inner` would otherwise
        # widen the whole block, push the right border off-screen, and desync
        # the column layout in hjoin. Every panel is exactly inner+2 wide.
        out.append(rgb(DIM2, "│") + _padcol(_clip(r, inner), inner) + rgb(DIM2, "│"))
    out.append(rgb(DIM2, "╰" + "─" * inner + "╯"))
    return out


def hjoin(*blocks, gap=3):
    blocks = [b for b in blocks if b]
    height = max((len(b) for b in blocks), default=0)
    widths = [max((_visible_len(x) for x in b), default=0) for b in blocks]
    out = []
    for i in range(height):
        out.append((" " * gap).join(
            _padcol(b[i] if i < len(b) else "", w) for b, w in zip(blocks, widths)))
    return out


TAB_WIN, TAB_AW = "__tab_win__", "__tab_aw__"
VIEW_LIVE, VIEW_HIST, VIEW_PRS = "__live__", "__history__", "__prs__"


def _menu_cell(label, ul_idx, on):
    """One Win3.1 menu tab: padded label, accelerator letter at `ul_idx`
    underlined. Active tab is highlighted (dark text on an accent block)."""
    fg = (18, 18, 28) if on else TEXT
    bg = ACCENT if on else None
    cell = " " + label + " "
    i = ul_idx + 1                         # +1 for the leading pad space
    return (styled(cell[:i], fg, bg, bold=on)
            + styled(cell[i], fg, bg, bold=on, underline=True)
            + styled(cell[i + 1:], fg, bg, bold=on))


def view_tabs(mode):
    """The Live / History / PRs menu-bar tabs (accelerator letters L/H/P
    underlined, active tab highlighted). Returns (styled, visible_len, segs)
    where segs = [(token, lo_off, hi_off)] are 0-based char offsets for the
    hit-map."""
    tabs = [(VIEW_LIVE, "Live", 0), (VIEW_HIST, "History", 0), (VIEW_PRS, "PRs", 0)]
    active = {"history": VIEW_HIST, "prs": VIEW_PRS}.get(mode, VIEW_LIVE)
    sep = " "
    out, off, segs = "", 0, []
    for i, (tok, lab, ul) in enumerate(tabs):
        if i:
            out += sep
            off += len(sep)
        width = len(lab) + 2               # padded cell width
        out += _menu_cell(lab, ul, tok == active)
        segs.append((tok, off, off + width - 1))
        off += width
    return out, off, segs


def summary_title(summary_tab):
    """Build the SUMMARY panel's tabbed title. Returns (styled, visible_len,
    segments) where segments = [(token, lo_off, hi_off)] are 0-based char
    offsets of each tab within the title text, for the click hit-map."""
    sep = " · "
    tabs = [(TAB_WIN, fmt_window(WINDOW)), (TAB_AW, fmt_window(ACTIVE_WINDOW))]
    active = TAB_AW if summary_tab == "aw" else TAB_WIN
    styled, off, segs = "", 0, []
    for i, (tok, lab) in enumerate(tabs):
        if i:
            styled += rgb(DIM2, sep)
            off += len(sep)
        on = tok == active
        styled += rgb(ACCENT if on else DIM, lab, bold=on)
        segs.append((tok, off, off + len(lab) - 1))
        off += len(lab)
    return styled, off, segs


def summary_rows(buckets, inner, win_label, lean=False, compact_nums=False,
                 show_cost=False):
    """Summary figures over `buckets` (the active tab's window slice).
    `win_label` names the window in labels. The effective-token figure is for
    that window only — the 1h/full split is gone now that the tabs select the
    window. `lean` drops the cache-mix breakdown to save rows on short
    terminals; `compact_nums` renders input/output as 55m rather than 55,123,456
    to narrow the panel. `show_cost` adds a $ estimate (effective tokens ×
    PRICE_PER_MTOK) and a cache-hit% line — used by the history view."""
    bignum = fmt_compact if compact_nums else fmt
    agg = empty_bucket()
    for b in buckets:
        for k in agg:
            agg[k] += b[k]
    total_input = agg["read"] + agg["new"] + agg["miss"]

    def kv(label, value):
        return label + " " * max(inner - _visible_len(label) - _visible_len(value), 1) + value

    def meter(key, name, value):
        return kv(rgb(CO[key], CHIP) + " " + rgb(TEXT, name), rgb(TEXT, value))

    eff = sum(eff_tokens(b["uncached"], b["c5m"], b["c1h"], b["read"], b["output"])
              for b in buckets)

    rows = [
        kv(rgb(DIM, "input"), rgb(TEXT, bignum(total_input), bold=True)),
        kv(rgb(DIM, "output"), rgb(TEXT, bignum(agg["output"]), bold=True)),
        kv(rgb(DIM, f"responses"), rgb(TEXT, fmt(agg["responses"]))),
        kv(rgb(DIM, f"effective"), rgb(TEXT, fmt_compact(round(eff)),
           bold=True)),
    ]
    if show_cost:
        cost = eff * PRICE_PER_MTOK / 1_000_000
        cost_str = f"${cost/1000:.1f}k" if cost >= 1000 else f"${cost:,.2f}"
        rows.append(kv(rgb(DIM, "$ est"), rgb(OK_C, cost_str, bold=True)))
        rows.append(kv(rgb(DIM, "cache hit"),
                       rgb(TEXT, pct(agg["read"], total_input))))
    if not lean:
        rows += [
            rgb(DIM2, "─" * inner),
            meter("c5m", "5m cache · subagent", pct(agg["c5m"], total_input)),
            meter("c1h", "1h cache · main", pct(agg["c1h"], total_input)),
            meter("read", "read from cache", pct(agg["read"], total_input)),
            meter("miss", "cache miss", pct(agg["miss"], total_input)),
        ]
    return rows


SESS_COL_W = {"c1h": 20, "c12h": 20, "ctx": 9}    # widths of the optional columns
SESS_FIXED_W = 8                                   # indent(2) + last(6)
IDENT_MAX, IDENT_MIN = 32, 12                       # session-name column range


def session_rows(sessions, now, inner, cols=("c1h", "c12h", "ctx"),
                 ident_w=IDENT_MAX):
    """Return (rows, active_sids). active_sids is the ordered list of session
    ids for the DATA rows (header excluded), so callers can map a clicked row
    index back to its session. `cols` selects which optional columns to show
    (they drop 12h, then 1h, then context on narrow terminals); `ident_w` is the
    session-name column width (truncated narrower to keep sessions+ctx visible)."""
    cutoff = now - ACTIVE_WINDOW
    active = sorted((s for s in sessions.values() if s["last_act"] >= cutoff),
                    key=lambda s: s["last_act"], reverse=True)
    heads = {"c1h": f"{'1h main/sub':<{SESS_COL_W['c1h']}}",
             "c12h": f"{'12h main/sub':<{SESS_COL_W['c12h']}}",
             "ctx": f"{'context':<{SESS_COL_W['ctx']}}"}
    rows = [rgb(DIM, f"  {'last':<6}{'session':<{ident_w}}"
                     + "".join(heads[c] for c in cols))]
    if not active:
        rows.append(rgb(DIM, f"  none"))
        return rows, []

    def bal(main, sub):
        tot = main + sub
        if tot <= 0:
            return rgb(DIM, "·")
        return (rgb(CO["main"], fmt_compact(main)) + rgb(DIM, "/")
                + rgb(CO["sub"], fmt_compact(sub)) + "  "
                + rgb(CO["sub"], f"{100 * sub / tot:.0f}%"))

    def ctx_cell(s):
        size = s["ctx"]
        if size <= 0:
            return rgb(DIM, "·")
        return ctx_dot(size, session_window(s), now) + " " + rgb(TEXT, fmt_compact(size))

    active_sids = []
    for s in active:
        active_sids.append(s["sid"])
        errored_last = s["err"] is not None and (
            s["last"] is None or s["err"]["ts"] >= s["last"])
        when_ts = s["last"] or s["last_act"]
        when = when_ts.astimezone().strftime("%H:%M")
        # Errored rows: red ! marker (2 cols, same as the plain indent), red
        # time. Everything else keeps its normal colour.
        indent = rgb(HOT_C, "! ") if errored_last else "  "
        when_col = (rgb(HOT_C, f"{when:<6}") if errored_last
                    else rgb(ACCENT, f"{when:<6}"))
        # Session column (always white): the /rename title if one exists; else
        # the project (cwd basename); else the session-id prefix. Truncated.
        proj = _clean(os.path.basename(s["cwd"])) if s["cwd"] else ""
        ident = s["name"] or proj or s["sid"][:8]
        ident_col = rgb(TEXT, f"{ident[:ident_w]:<{ident_w}}")
        cells = {"c1h": lambda: _padcol(bal(s["main_1h"], s["sub_1h"]), SESS_COL_W["c1h"]),
                 "c12h": lambda: _padcol(bal(s["main_12"], s["sub_12"]), SESS_COL_W["c12h"]),
                 "ctx": lambda: _padcol(ctx_cell(s), SESS_COL_W["ctx"])}
        rows.append(indent + when_col + ident_col
                    + "".join(cells[c]() for c in cols))
    return rows, active_sids


# ── per-session popup ─────────────────────────────────────────────────────────

def render_popup(sid, sessions, now, cols, rows, anim=0):
    """A bordered modal showing one session's own 3 charts. Returns the list of
    lines, or None if the session is gone. cols/rows accepted for symmetry with
    the caller (sizing is fixed to MARGIN + NUM_BUCKETS so the charts fit)."""
    s = sessions.get(sid)
    if s is None:
        return None
    inner = MARGIN + NUM_BUCKETS          # 152 — same chart geometry as main
    sb = s["buckets"]
    proj = _clean(os.path.basename(s["cwd"]) or "?")
    size = s["ctx"]
    if size > 0:
        ctx_str = (ctx_dot(size, session_window(s), now) + " "
                   + rgb(TEXT, fmt_compact(size) + " ctx"))
    else:
        ctx_str = rgb(DIM, "· no main turn")
    if s["peak_sub"] > 0:
        sub_str = (rgb(DIM, "  ·  sub peak ") + ctx_dot(s["peak_sub"], sub_window(s), now)
                   + " " + rgb(TEXT, fmt_compact(s["peak_sub"])))
    else:
        sub_str = rgb(DIM, "  ·  no subagents")
    name_str = rgb(ACCENT2, s["name"], bold=True) + rgb(DIM, "  ") if s["name"] else ""
    head = (rgb(TEXT, proj, bold=True) + rgb(DIM, "  ") + name_str
            + rgb(DIM, s["sid"][:8] + "  ")
            + rgb(ACCENT, short_model(s["model"])) + rgb(DIM, "  ")
            + ctx_str + sub_str)

    # Effective-tokens summary: main (blue) vs sub (purple), 1h and 12h.
    eff_line = (
        rgb(DIM, "effective   ")
        + rgb(CO["main"], "main") + rgb(DIM, " 1h ")
        + rgb(TEXT, fmt_compact(round(s["eff_main_1h"])))
        + rgb(DIM, " · 12h ") + rgb(TEXT, fmt_compact(round(s["eff_main_12"])))
        + rgb(DIM, "    ")
        + rgb(CO["sub"], "sub") + rgb(DIM, " 1h ")
        + rgb(TEXT, fmt_compact(round(s["eff_sub_1h"])))
        + rgb(DIM, " · 12h ") + rgb(TEXT, fmt_compact(round(s["eff_sub_12"]))))

    body = [head, eff_line]
    if s["err"]:
        st = s["err"].get("status")
        st_str = f"HTTP {st}" if st is not None else "error"
        msg_txt = (s["err"].get("text") or "").replace("\n", " ").strip()
        prefix = (rgb(HOT_C, "⚠ last error · " + st_str, bold=True)
                  + rgb(DIM, " · "))
        room = inner - _visible_len(prefix)
        if room > 1 and len(msg_txt) > room:
            msg_txt = msg_txt[:max(room - 1, 0)] + "…"
        body.append(prefix + rgb(TEXT, msg_txt))
    body += [""]
    # Charts compact exactly like the main view: same breakpoints (drop tick row
    # and fold title+legend below 39 rows, drop the baseline below 24), and the
    # bar height shrinks to fit. Reserve ~header + subagent-table chrome; what
    # doesn't fit still scrolls in the overlay viewport.
    c = chart_compaction(rows)
    chrome = 3 + 4 + 4                    # header(3) + subagent(~4) + borders/footer(4)
    nonbar = (1 if c["compact"] else 2) + (1 if c["axes"] else 0)
    ch = max(MIN_BAR_H, min(5, (rows - chrome - 3 * nonbar) // 3))
    block, _ = chart_block(sb, ch, c["compact"], c["axes"], c["blanks"], now, anim)
    body += block

    # Named subagent detail table — subagents active in the lookback window only.
    cutoff = now - ACTIVE_WINDOW
    win = fmt_window(ACTIVE_WINDOW)
    cands = sorted((sub for sub in s["subs"].values() if sub["stop"] >= cutoff),
                   key=lambda sub: sub["start"])
    body += ["", "  " + rgb(DIM, f"subagents · last {win}")]
    if not cands:
        body += ["  " + rgb(DIM, f"no subagents in the last {win}")]
    else:
        def sub_row(sub):
            slug = sub["slug"][:26]
            start = sub["start"].astimezone().strftime("%H:%M")
            stop = sub["stop"].astimezone().strftime("%H:%M")
            cw = window_for(sub.get("model"), sub["peak"])
            dot = ctx_dot(sub["peak"], cw, now)
            return ("  " + _padcol(rgb(TEXT, slug), 28)
                    + _padcol(rgb(ACCENT, short_model(sub.get("model"))), 14)
                    + _padcol(rgb(DIM, start), 8) + _padcol(rgb(DIM, stop), 8)
                    + _padcol(dot + " " + rgb(TEXT, fmt_compact(sub["peak"])), 12)
                    + rgb(TEXT, fmt_compact(round(sub["eff"]))))
        header = ("  " + _padcol(rgb(DIM, "subagent"), 28)
                  + _padcol(rgb(DIM, "model"), 14)
                  + _padcol(rgb(DIM, "start"), 8) + _padcol(rgb(DIM, "stop"), 8)
                  + _padcol(rgb(DIM, "peak ctx"), 12) + rgb(DIM, "eff tkn"))
        # All subagents shown in timeline order; the overlay viewport scrolls if
        # the popup is taller than the terminal.
        body += [header] + [sub_row(sub) for sub in cands]

    body += ["", "  " + rgb(DIM, "click outside · q · esc to close")]
    return panel("SESSION DETAIL", body, inner)


def render_bucket_popup(idx, sessions, now, cols, rows):
    """A bordered modal breaking one chart bar (a single bucket / time slice)
    down by session, covering all three chart dimensions: input (uncached new
    in / 5m write / 1h write), context (cache hit / miss), output, and
    effective tokens. Returns the list of lines, or None if out of range."""
    if not (0 <= idx < NUM_BUCKETS):
        return None
    inner = 92
    cutoff = now - VIEW_WINDOW
    start = (cutoff + idx * VIEW_BUCKET).astimezone()
    end = (cutoff + (idx + 1) * VIEW_BUCKET).astimezone()
    span = (f"{start:%a %H:%M}–{end:%H:%M}" if VIEW_DAILY
            else f"{start:%H:%M}–{end:%H:%M}")

    agg = empty_bucket()
    entries = []                         # (label, bucket, eff, model)
    for s in sessions.values():
        b = s["buckets"][idx]
        if b["uncached"] + b["c5m"] + b["c1h"] + b["read"] + b["output"] <= 0:
            continue
        for k in agg:
            agg[k] += b[k]
        label = (s["name"] or _clean(os.path.basename(s["cwd"]) or "")
                 or s["sid"][:8])
        e = eff_tokens(b["uncached"], b["c5m"], b["c1h"], b["read"], b["output"])
        entries.append((label, b, e, s.get("model")))
    entries.sort(key=lambda e: e[2], reverse=True)

    head = (rgb(TEXT, f"bucket {span}", bold=True)
            + rgb(DIM, f"   ·   {fmt_window(VIEW_BUCKET)} slice   ·   "
                       f"{len(entries)} session{'' if len(entries) == 1 else 's'}"))

    # session(26) in/5m/1h/hit/miss/out(9 each) eff -> ~92 inner.
    W = 9
    def row(label, b, eff, lab_style):
        def c(v):
            return rgb(TEXT, fmt_compact(round(v)))
        return ("  " + _padcol(lab_style(label[:24]), 26)
                + _padcol(c(b["uncached"]), W)
                + _padcol(c(b["c5m"]), W)
                + _padcol(c(b["c1h"]), W)
                + _padcol(c(b["read"]), W)
                + _padcol(c(b["miss"]), W)
                + _padcol(c(b["output"]), W)
                + rgb(TEXT, fmt_compact(round(eff)), bold=True))

    def h(t):
        return _padcol(rgb(DIM, t), W)
    header = ("  " + _padcol(rgb(DIM, "session"), 26)
              + h("in") + h("5m") + h("1h") + h("hit") + h("miss") + h("out")
              + rgb(DIM, "eff"))

    body = [head, ""]
    if not entries:
        body += [rgb(DIM, "  no activity in this slice")]
    else:
        body += [header]
        agg_eff = eff_tokens(agg["uncached"], agg["c5m"], agg["c1h"],
                             agg["read"], agg["output"])
        # All sessions shown; the overlay viewport scrolls if it's taller than
        # the terminal.
        body += [row(lab, b, e, lambda t: rgb(TEXT, t)) for lab, b, e, _ in entries]
        body += [rgb(DIM2, "─" * inner),
                 row("all sessions", agg, agg_eff,
                     lambda t: rgb(TEXT, t, bold=True))]

    body += ["", "  " + rgb(DIM, "click outside · q · esc to close")]
    return panel("BUCKET BREAKDOWN", body, inner)


def render_panel_popup(view, buckets, sessions, now, rows, summary_tab):
    """One of the side panels rendered as a modal (used on terminals too
    narrow/short to show them inline). Returns (lines, regions) where regions =
    [(overlay_line, lo_off, hi_off, token)] are clickable spans in coordinates
    relative to the overlay box; the caller offsets them by the box origin.
    The history view contributes "hsummary" (window summary + $ cost) and
    "heatmap" (the activity grid)."""
    if view == "hsummary":              # history SUMMARY popup (with $ cost)
        inner = SUMM_FULL
        rows_ = summary_rows(buckets, inner, fmt_window(HIST_WINDOW),
                             show_cost=True)
        return panel("SUMMARY · last " + fmt_window(HIST_WINDOW), rows_, inner), []
    if view == "heatmap":               # history ACTIVITY heatmap popup
        p, _ = heatmap_panel(HIST_HEAT)
        return p, []
    if view == "summary":
        inner = 40
        p, segs = summary_panel(buckets, summary_tab, inner)
        # Tabs sit on the title border (overlay line 0); text starts at offset 3
        # after the "╭─ " prefix.
        return p, [(0, 3 + lo, 3 + hi, tok) for tok, lo, hi in segs]
    if view == "sessions":
        inner = 92
        sess_rows, active_sids = session_rows(sessions, now, inner)
        maxdata = max(rows - 4, 1)              # borders + header + a little air
        if len(active_sids) > maxdata:
            sess_rows = sess_rows[:1 + maxdata]
            active_sids = active_sids[:maxdata]
        p = panel(f"ACTIVE SESSIONS · last {fmt_window(ACTIVE_WINDOW)}",
                  sess_rows, inner)
        # data row j -> overlay line 2+j (line 0 border, line 1 header).
        return p, [(2 + j, 1, inner, sid) for j, sid in enumerate(active_sids)]
    if view == "allow":
        inner = 26
        p = panel("ALLOWANCE",
                  allowance_rows(now, int(now.timestamp()), inner), inner)
        return p, []
    return None, []


def render_help(now, cols, rows):
    """A modal explaining every element of the dashboard and how to read it.

    Word-wrapped to fit the width; the overlay viewport scrolls it when taller
    than the screen. Content is authored as typed items so colour survives
    wrapping:
      ("H",  text)      heading  (cyan, bold; never wraps — keep short)
      ("L",  text)      legend   (pre-coloured single line; never wraps)
      ("T",  text)      prose    (plain str, single colour; wrapped + DIM'd)
      ("G",  None)      gap      (one blank line)

    Returns the list of panel lines."""
    inner = max(min(int(cols * 0.75), 80) - 2, 30)   # content width inside borders
    twidth = inner - 1                         # wrap prose, leaving a col for the
    #                                            scrollbar fit_overlay may add.

    def ch(k):                                  # colour chip for a palette key
        return rgb(CO[k], CHIP)

    items = [
        ("T", f"Live Claude Code cache-token usage, last {fmt_window(WINDOW)} in "
              f"{fmt_window(BUCKET)} buckets. Charts refresh every "
              f"{max(1, INTERVAL_SECONDS // 60)}m; the screen animates."),
        ("G", None),
        ("H", "CHARTS"),
        ("T", "y-axis = tokens, x-axis = clock hour."),
        ("L", "Chart 1  Input · cache-write:   "
              + ch("uncached") + rgb(DIM, " uncached  ")
              + ch("c5m") + rgb(DIM, " 5m=subagent  ")
              + ch("c1h") + rgb(DIM, " 1h=main")),
        ("L", "Chart 2  Context assembly:      "
              + ch("read") + rgb(DIM, " from cache  ")
              + ch("new") + rgb(DIM, " new  ")
              + ch("miss") + rgb(DIM, " miss*")),
        ("L", "Chart 3  Output:                " + ch("output") + rgb(DIM, " output tokens")),
        ("G", None),
        ("T", "*: miss = a turn that read 0 from cache (the cached prefix expired "
              "in an idle gap, or it is the session's 1st turn)."),
        ("G", None),
        ("H", "EFFECTIVE TOKENS"),
        ("T", "True cost in token-equivalents. Formula: 1x uncached + "
              "1.25x 5m-write + 2x 1h-write + 0.1x cache-read + 5x output."),
        ("G", None),
        ("H", "SUMMARY"),
        ("T", "Totals for the window: input, output, responses, effective tokens, "
              "plus the cache-mix chips. Click the title TABS to switch between "
              "the full window and the active window."),
        ("G", None),
        ("H", "ACTIVE SESSIONS"),
        ("T", f"Sessions active in the last {fmt_window(ACTIVE_WINDOW)} "
              "(--active-window). A renamed session (/rename) shows its title in "
              "place of the id. \"1h / 12h\" = fresh-token split, main vs subagent. "
              "A session turns RED with a ! when its most recent action hit a "
              "surfaced API error."),
        ("G", None),
        ("H", "CONTEXT LIGHT"),
        ("L", rgb(OK_C, "●") + rgb(DIM, " green   ") + rgb(WARN_C, "●")
              + rgb(DIM, " yellow   ") + rgb(ORANGE_C, "●") + rgb(DIM, " amber   ")
              + rgb(HOT_C, "●") + rgb(DIM, " red   ") + rgb(HOT_C, "→ flashing red")),
        ("T", "context = latest main turn's size; thresholds scale to the window, "
              "taken from the model (Opus/Sonnet are 1M-capable, others 200k; any "
              "session ever seen above 200k is treated as 1M).  200k window: "
              "green <=100k · yellow <=125k · amber <=150k · red <=175k · flashing "
              ">175k.  1M window: green <=150k · yellow <=300k · amber <=450k · "
              "red <=600k · flashing >600k."),
        ("G", None),
        ("H", "ALLOWANCE"),
        ("T", "Live subscription usage (/usage): 5-hour session + weekly gauges "
              "with reset time. Gauge: green <=70% · yellow <=80% · amber <=90% · "
              "red <=95% · flashing >95%. Click the panel when it errors to see the "
              "response body."),
        ("G", None),
        ("H", "CLICK A SESSION"),
        ("T", f"Opens its detail: that session's 3 charts, effective tokens "
              f"(main/sub, 1h & 12h), named subagents from the last "
              f"{fmt_window(ACTIVE_WINDOW)} (peak ctx + eff tkn), and any recent "
              "error."),
        ("G", None),
        ("H", "CLICK A BAR"),
        ("T", "Opens a breakdown of that one time bucket by session: new input, "
              "cache writes, cache reads, output, and effective tokens."),
        ("G", None),
        ("H", "SMALL TERMINALS"),
        ("T", "The layout degrades to fit: charts compact, panels move off-screen. "
              "On a narrow/short terminal press s / e / w to open the SUMMARY / "
              "active-sessions / allowance panels as popups."),
        ("G", None),
        ("H", "HISTORY"),
        ("T", f"Press H (or click the History tab / footer span) for a longer-span "
              f"view — default last {fmt_window(HIST_WINDOW)}, configurable via "
              "--history-hours. The three charts plus a 4th stacking effective "
              "tokens by model, all with a coarser auto-scaled bucket and a "
              "day-by-day axis; alongside a SUMMARY ($ cost estimate + cache-hit "
              "rate) and an ACTIVITY heatmap (effective tokens by weekday × "
              "hour). Click a bar to break the slice down by session. On a small "
              "screen the two panels move to popups — press S (summary) / M "
              "(heatmap). L (or q) returns to the Live tab."),
        ("G", None),
        ("H", "KEYS"),
        ("T", "? help · L live / H history tabs · S/M history popups · "
              "s/e/w live panels · click bar/session/tab · "
              "up/down PgUp/PgDn j/k scroll · q / esc step back · ^C quit."),
    ]

    # Flatten to coloured lines. Headings/legends/gaps -> one line; prose ->
    # one DIM-coloured line per wrapped piece (single colour, so wrapping the
    # plain string and re-colouring each line keeps colour intact).
    lines = []
    for kind, text in items:
        if kind == "H":
            lines.append(rgb(ACCENT, text, bold=True))
        elif kind == "L":
            lines.append(text)
        elif kind == "G":
            lines.append("")
        else:                                   # "T"
            for piece in _wrap(text, twidth):
                lines.append(rgb(DIM, piece))

    # Full panel; the overlay viewport (fit_overlay) handles scrolling + the
    # scrollbar so this fits any terminal height.
    return panel("HELP · how to read this dashboard", lines, inner)


def _wrap(text, width):
    """Word-wrap `text` to `width` cols, hard-breaking tokens longer than width.
    Returns a list of lines (never wider than width)."""
    if width <= 0:
        return [text]
    lines, cur = [], ""
    for tok in text.split():
        while len(tok) > width:                # hard-break an oversized token
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(tok[:width])
            tok = tok[width:]
        if not cur:
            cur = tok
        elif len(cur) + 1 + len(tok) <= width:
            cur += " " + tok
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    return lines or [""]


def render_usage_error(now, cols, rows):
    """Overlay detailing the last failed live-usage (/usage) call: status,
    endpoint, and the response body word-wrapped. None if there's nothing to
    show."""
    err = _usage.get("err")
    if not err or err == "loading…":
        return None
    inner = min(80, max(cols - 4, 40))
    lines = [rgb(HOT_C, err[:inner], bold=True), rgb(DIM, USAGE_URL[:inner]), ""]
    body = _clean(_usage.get("err_body") or "(no body captured)")
    wrapped = _wrap(body, inner)
    # Cap so the whole panel (2 borders + footer block of 2 + these head lines)
    # fits `rows`. Reserve the head lines already in `lines`, the footer, and
    # one row for a possible truncation note.
    actions = rgb(WARN_C, "[R]") + rgb(DIM, " retry now")
    if _token_stale():
        actions += rgb(DIM, "   ·   ") + rgb(WARN_C, "[L]") + rgb(DIM, " login")
    footer = ["", actions, rgb(DIM, "click outside · q · esc to close")]
    overhead = 2 + len(lines) + len(footer)     # 2 panel borders
    room = max(rows - overhead, 1)
    truncated = False
    if len(wrapped) > room:
        wrapped = wrapped[:max(room - 1, 0)]
        truncated = True
    lines += wrapped
    if truncated:
        lines.append(rgb(DIM, "… (truncated)"))
    lines += footer
    return panel("USAGE CALL ERROR", lines, inner)


ACCT_COL_W = (20, 13, 8, 10)   # account, expiry, status, action — text widths
ACCT_PAD = 1                   # spaces between a column's text and its gridlines


def _acct_col_starts():
    """Absolute char offsets (within the table row string) where each
    column's TEXT begins — i.e. past its leading pad space and any grid
    lines/padding of the columns before it."""
    starts, pos = [], 0
    for w in ACCT_COL_W:
        pos += ACCT_PAD
        starts.append(pos)
        pos += w + ACCT_PAD + 1     # text + trailing pad + gridline
    return starts


def _acct_row(cells):
    """cells: [(text, w, color, bold), ...] — one per column, already sized to
    ACCT_COL_W. Joins them with 1-space-padded gridlines."""
    sep = rgb(DIM2, "│")
    return sep.join(rgb(color, " " + text.ljust(w) + " ", bold=bold)
                     for text, w, color, bold in cells)


def _progress_content(inner, elapsed, label, cancel_token=None):
    """Shared 'still working' body: centered label, centered Cylon bar, and
    (if cancel_token given) a clickable [Cancel] link plus a dismiss hint.
    Returns (content_lines, regions) for panel(...)."""
    bar_w = min(inner - 4, 20)
    bar = _cylon_bar(elapsed, bar_w)
    bar_pad = max(0, (inner - bar_w) // 2)
    content = [
        "",
        rgb(TEXT, label.center(inner), bold=True),
        "", " " * bar_pad + bar, "",
    ]
    regions = []
    if cancel_token is not None:
        cancel = "[Cancel]"
        cancel_pad = max(0, (inner - len(cancel)) // 2)
        content.append(" " * cancel_pad + rgb(WARN_C, cancel, bold=True))
        regions.append((len(content), cancel_pad + 1, cancel_pad + len(cancel),
                        cancel_token))
        content += ["", rgb(DIM, "esc / click outside also cancels".center(inner))]
    return content, regions


def render_loading(now, cols, rows, elapsed):
    """Full-screen-modal 'assembling data' popup for the very first transcript
    scan (kick_collect("live", ...)) — the only time there's nothing else on
    screen to show progress. Never shown again after that first scan finishes;
    later refreshes run in the background with no popup (they don't block
    input any more, so there's nothing to explain)."""
    starts = _acct_col_starts()
    inner = starts[-1] + ACCT_COL_W[-1] + ACCT_PAD
    content, regions = _progress_content(
        inner, elapsed, "Assembling data from your Claude Code history…")
    return panel("LOADING", content, inner), regions


def render_login_confirm(now, cols, rows, login_elapsed=None):
    """Account-switch modal: a table of saved accounts, one row each. Status
    is 'Current' (that row's account is the live one) or a '[Select]' link
    (instant switch, no TUI suspend — a file swap); '[Re-login]' switches
    that row live THEN starts `claude auth login` in the background, so it
    also works to refresh an expired non-current account. '[+] add account'
    logs in immediately with no extra confirm; the result becomes live (it's
    what `claude auth login` just wrote) and gets snapshotted into the table
    on return. Returns (lines, regions) — regions = [(overlay_line, lo, hi,
    token)] in the same convention as render_panel_popup.

    While a login is running (login_elapsed is not None, seconds since it
    started), the table is replaced with the Cylon progress bar and a
    [Cancel] link — the caller (main loop) owns the actual subprocess.

    This is the shared OAuth store (~/.claude/.credentials.json) — the same
    login Claude Code itself reads — so a switch here is global, not local;
    other running Claude Code sessions pick it up silently on their next
    prompt, no restart needed."""
    aw, ew, sw, cw = ACCT_COL_W
    starts = _acct_col_starts()
    inner = starts[-1] + cw + ACCT_PAD - 0   # last column's text-end + trailing pad
    if login_elapsed is not None:
        content, regions = _progress_content(
            inner, login_elapsed, "Logging in…", cancel_token="__logincancel__")
        return panel("SWITCH ACCOUNT", content, inner), regions
    saved = list_saved_accounts()
    cur_slug = current_account_slug()
    if cur_slug is None:
        # The live account hasn't landed on disk yet (save_account_snapshot
        # runs opportunistically off the periodic profile fetch) — show it
        # anyway from what's already fetched, so the table is never wrongly
        # empty just because nothing's been snapshotted yet. Slug "" (not a
        # real file) marks it: [Select] never renders for it (is_cur below),
        # and [Re-login] on it is do_login with no do_switch (there's
        # nothing on disk to switch to).
        live_label = _usage.get("account")
        if live_label:
            try:
                live_exp = _expiry_label(json.load(open(CREDS_PATH)))
            except (OSError, ValueError):
                live_exp = ""
            saved = [("", live_label, live_exp)] + saved

    content = [_acct_row([("ACCOUNT", aw, TEXT, True), ("EXPIRY", ew, TEXT, True),
                          ("STATUS", sw, TEXT, True), ("ACTION", cw, TEXT, True)]),
               rgb(DIM2, "─" * (aw + 2 * ACCT_PAD) + "┼" + "─" * (ew + 2 * ACCT_PAD)
                   + "┼" + "─" * (sw + 2 * ACCT_PAD) + "┼" + "─" * (cw + 2 * ACCT_PAD))]
    regions = []
    if not saved:
        content.append(rgb(DIM, "No saved accounts yet."))
    for slug, label, exp in saved:
        is_cur = slug == cur_slug or slug == ""
        status_plain = "Current" if is_cur else "[Select]"
        action_plain = "[Re-login]"
        content.append(_acct_row([
            (_clip(label, aw), aw, TEXT, False),
            (exp or "-", ew, DIM, False),
            (status_plain, sw, ACCENT if is_cur else WARN_C, is_cur),
            (action_plain, cw, WARN_C, False),
        ]))
        li = len(content)
        status_start, action_start = starts[2], starts[3]
        if not is_cur:
            regions.append((li, status_start + 1, status_start + len(status_plain),
                             f"__acctsel__{slug}"))
        regions.append((li, action_start + 1, action_start + len(action_plain),
                         f"__acctrelogin__{slug}"))
    content.append("")
    content.append(rgb(WARN_C, "[+] add account", bold=True))
    regions.append((len(content), 1, inner, "__acctadd__"))
    content.append("")
    content.append(rgb(DIM, "esc / click outside to cancel"))
    return panel("SWITCH ACCOUNT", content, inner), regions


# ── live bundled-allowance usage (GET /api/oauth/usage, same as `/usage`) ─────

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
ACCOUNTS_DIR = os.path.expanduser("~/.claude/dashboard-accounts")
# Shared by the context light (ctx_grade) and allowance gauge (gauge_grade) —
# the actual thresholds live in those functions, not here.
OK_C = (52, 224, 150)       # green
WARN_C = (255, 205, 82)     # yellow
HOT_C = (255, 88, 96)       # red
ORANGE_C = (255, 138, 56)   # amber

# Shared with the render thread; the network call must never block a frame.
_usage = {"data": None, "err": "loading…", "at": None, "sub": None, "tier": None,
          "retry_at": None, "err_body": None, "account": None}
_usage_inflight = threading.Lock()


def _usage_set(**changes):
    """Atomically publish new usage state. Rebinds the module dict to a merged
    copy rather than mutating in place, so a reader that snapshots `_usage` once
    sees a CONSISTENT set of keys — the daemon fetch thread never mutates the
    dict a render is currently reading. `retry_at` is owned solely by fetch_usage
    (cleared on success, set on failure); the main loop only reads it."""
    global _usage
    _usage = {**_usage, **changes}


def _retry_after_secs(e):
    """Honour a 429/503 `Retry-After` header (integer seconds) if present and
    sane; otherwise fall back to the flat USAGE_BACKOFF. HTTP-date form is not
    parsed — we just use the default for that."""
    try:
        hdr = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
    except Exception:
        hdr = ""
    if hdr.isdigit():
        return max(USAGE_REFRESH, min(int(hdr), 3600))   # clamp to [5m, 1h]
    return USAGE_BACKOFF


def _oauth_headers(tok):
    """Shared headers for the OAuth-scoped GETs (usage + profile)."""
    return {
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-cli/cache-monitor",
    }


def _fetch_account(tok, timeout):
    """Best-effort: the signed-in account's email for the top-right corner. Run
    after a successful usage fetch; a failure here must NOT fail the usage call,
    so it's swallowed (the last-known account stays on screen)."""
    try:
        req = urllib.request.Request(PROFILE_URL, headers=_oauth_headers(tok))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            acct = (json.load(r).get("account") or {})
        label = acct.get("email") or acct.get("display_name")
        _usage_set(account=label)
    except Exception:
        log.info("fetch_usage: profile fetch failed (non-fatal)", exc_info=True)
        return
    try:
        # Opportunistic: the SWITCH ACCOUNT table needs the current account on
        # disk to list it. No extra network call — reuses the label just
        # fetched above; save_account_snapshot dedupes if already saved.
        save_account_snapshot(label=label)
    except Exception:
        log.info("fetch_usage: account snapshot failed (non-fatal)", exc_info=True)


def fetch_usage(timeout=15):
    """Read the current OAuth token from the creds file (so token refreshes by
    Claude Code are picked up) and GET the live utilisation. Last-good wins.
    Single-flight is enforced by the caller (kick_usage); --once calls directly
    and is single-threaded."""
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    back = lambda s: now + timedelta(seconds=s)
    try:
        log.info("fetch_usage: start")
        oa = (json.load(open(CREDS_PATH)).get("claudeAiOauth") or {})
        tok = oa.get("accessToken")
        if not tok:
            _usage_set(err="no oauth token", err_body=None, retry_at=back(USAGE_BACKOFF))
            log.warning("fetch_usage: no oauth token in %s", CREDS_PATH)
            return
        req = urllib.request.Request(USAGE_URL, headers=_oauth_headers(tok))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            data = json.load(r)
        _usage_set(data=data, err=None, retry_at=None, at=datetime.now(timezone.utc),
                   sub=oa.get("subscriptionType"), tier=oa.get("rateLimitTier"),
                   err_body=None)
        _fetch_account(tok, timeout)
        log.info("fetch_usage: ok HTTP %s in %.2fs, limits=%d spend=%s",
                 status, time.monotonic() - t0,
                 len(data.get("limits", [])), bool(data.get("spend")))
    except urllib.error.HTTPError as e:
        try:                                # e.read() works once; guard it
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        secs = _retry_after_secs(e)
        _usage_set(err=f"HTTP {e.code}" + (" (token stale)" if e.code == 401 else ""),
                   err_body=body[:4000], retry_at=back(secs))
        log.warning("fetch_usage: HTTPError %s after %.2fs, backoff %ss",
                    e.code, time.monotonic() - t0, secs)
    except Exception as e:
        _usage_set(err=type(e).__name__, err_body=str(e)[:4000], retry_at=back(USAGE_BACKOFF))
        log.exception("fetch_usage: failed after %.2fs", time.monotonic() - t0)


def kick_usage():
    """Fire a non-blocking background refresh if one isn't already running."""
    if _usage_inflight.acquire(blocking=False):
        def run():
            try:
                fetch_usage()
            finally:
                _usage_inflight.release()
        threading.Thread(target=run, daemon=True).start()
    else:
        log.info("kick_usage: skipped, refresh already in flight")


# collect() is a synchronous filesystem scan (can run seconds on a slow disk
# or many transcripts) — run it on a background thread so the render loop's
# input-polling select() never blocks on it, for the first load same as every
# periodic refresh. One in-flight scan per mode ("live"/"history"); the render
# thread only ever reads _collect_result[mode], a (buckets, sessions) tuple
# swapped in whole by an assignment (atomic under the GIL) — never the dict
# collect() is still mutating, so no torn/resizing-during-iteration reads.
_collect_inflight = {"live": threading.Lock(), "history": threading.Lock()}
_collect_result = {}   # mode -> (buckets, sessions), last snapshot published


def kick_collect(mode, now, window=None, bucket=None, num_buckets=None,
                 track_models=False, track_heatmap=False):
    """Fire a non-blocking background collect() for `mode` if one isn't
    already running; no-op otherwise (the running scan will publish soon)."""
    lock = _collect_inflight[mode]
    if not lock.acquire(blocking=False):
        return
    prev = _collect_result.get(mode)
    seed = dict(prev[1]) if prev else None   # thread's own copy to mutate
    first_publish = True

    def publish(b, s):
        # collect()'s very first progress_cb call fires before any file is
        # read, with all-zero buckets — meant to give the true first-ever
        # load something to animate behind the LOADING popup. On a refresh
        # (prev already published), publishing that zero snapshot would
        # flash the charts/SUMMARY to 0 every --interval until the rescan
        # refills them (buckets, unlike sessions, aren't seeded — a bucket
        # sums contributions from many session files, so there's no clean
        # per-bucket carry-over). Skip only that one pre-scan call on a
        # refresh; every later call has real (if still-accumulating) data.
        nonlocal first_publish
        skip = first_publish and prev is not None
        first_publish = False
        if skip:
            return
        _collect_result[mode] = (list(b), dict(s))

    def run():
        try:
            collect(now, window, bucket, num_buckets, track_models,
                    track_heatmap, progress_cb=publish, seed_sessions=seed)
        except Exception:
            log.exception("kick_collect(%s): scan failed", mode)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()


# ── PRS tab: open PRs + unopened contributed branches, via `gh` CLI ─────────
# Optional subsystem — degrades to a static message if `gh` isn't installed or
# not authenticated, never touches LIVE/HISTORY. No hardcoded org/owner: PRs
# come from `gh search prs --author=@me` (org-agnostic), and the branch scan
# starts from `search/commits?q=author:<login>` to discover repos actually
# touched, so it works for any GitHub account.
_GH_BIN = shutil.which("gh")
PR_REFRESH_SECONDS = 300
_pr_username = None
_pr_collect_inflight = threading.Lock()
_pr_collect_result = {}   # "prs" -> (rows, err_str_or_None)
_pr_action = {"running": False, "kind": None, "row_key": None,
              "started": None, "error": None}


def _gh_json(args, timeout=20):
    """Run `gh` with the given args, parse stdout as JSON. None on any failure
    (logged, never raised) — one bad call must not abort the whole scan."""
    if not _GH_BIN:
        return None
    try:
        out = subprocess.run([_GH_BIN] + args, capture_output=True, text=True,
                             timeout=timeout)
        if out.returncode != 0:
            log.warning("gh %s: %s", args, out.stderr.strip()[:200])
            return None
        return json.loads(out.stdout) if out.stdout.strip() else None
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        log.warning("gh %s: %s", args, e)
        return None


def gh_username():
    global _pr_username
    if _pr_username is None and _GH_BIN:
        user_obj = _gh_json(["api", "user"], timeout=10)
        _pr_username = user_obj.get("login") if user_obj else None
    return _pr_username


def _pr_ci_status(rollup):
    """statusCheckRollup -> ("green"/"red"/"pending"/"none", [(name, conclusion)])."""
    if not rollup:
        return "none", []
    checks = [(c.get("name") or c.get("context") or "?", c.get("conclusion") or c.get("state"))
              for c in rollup]
    concl = [c[1] for c in checks]
    if any(c in ("FAILURE", "failure", "ERROR", "error", "CANCELLED", "cancelled") for c in concl):
        return "red", checks
    if any(c in (None, "PENDING", "pending", "IN_PROGRESS", "in_progress", "QUEUED") for c in concl):
        return "pending", checks
    return "green", checks


def _pr_approval(review_decision):
    return {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes requested",
            "REVIEW_REQUIRED": "Awaiting review"}.get(review_decision, "No review needed")


def collect_prs():
    """Synchronous scan (background-threaded by kick_collect_prs): open PRs
    authored by the signed-in user, plus branches with no open PR whose latest
    commit is also theirs. Returns (rows, err) — err is a user-facing string
    when the whole tab should show a message instead of a table."""
    if not _GH_BIN:
        return [], "gh CLI not found — install from https://cli.github.com"
    user = gh_username()
    if not user:
        return [], "gh not authenticated — run `gh auth login`"

    rows, seen = [], set()   # seen: (repo, branch) already surfaced as a PR
    prs = _gh_json(["search", "prs", "--author=@me", "--state=open",
                    "--json", "repository,number,title,url,updatedAt"]) or []
    for p in prs:
        repo = p.get("repository")
        repo = repo.get("nameWithOwner") if isinstance(repo, dict) else repo
        if not repo:
            continue
        num = p["number"]
        detail = _gh_json(["pr", "view", str(num), "--repo", repo, "--json",
                           "reviewDecision,statusCheckRollup,commits,comments,"
                           "headRefName,isDraft,url,title"]) or {}
        commits = detail.get("commits") or []
        last_commit = commits[-1] if commits else {}
        comments = detail.get("comments") or []
        last_comment = comments[-1] if comments else None
        ci_state, ci_checks = _pr_ci_status(detail.get("statusCheckRollup"))
        rows.append({
            "kind": "pr", "repo": repo, "number": num,
            "branch": detail.get("headRefName", ""),
            "title": detail.get("title") or p.get("title") or "",
            "url": detail.get("url") or p.get("url") or "",
            "is_draft": bool(detail.get("isDraft")),
            "approval": _pr_approval(detail.get("reviewDecision")),
            "ci": ci_state, "ci_checks": ci_checks,
            "commit_ts": parse_ts(last_commit.get("committedDate")) if last_commit.get("committedDate") else None,
            "commit_sha": (last_commit.get("oid") or "")[:7],
            "commit_msg": (last_commit.get("messageHeadline") or ""),
            "comment_ts": parse_ts(last_comment["createdAt"]) if last_comment else None,
            "comment_author": (last_comment.get("author", {}) or {}).get("login", "") if last_comment else "",
            "comment_preview": (last_comment.get("body") or "")[:20] if last_comment else "",
            "comment_full": (last_comment.get("body") or "") if last_comment else "",
        })
        seen.add((repo, detail.get("headRefName", "")))

    commits_hits = _gh_json(["api", "search/commits", "-f", f"q=author:{user}"], timeout=25) or {}
    repos = sorted({(it.get("repository") or {}).get("full_name")
                    for it in (commits_hits.get("items") or [])
                    if (it.get("repository") or {}).get("full_name")})
    for repo in repos:
        repo_obj = _gh_json(["api", repo], timeout=10)
        default_branch = repo_obj.get("default_branch") if repo_obj else None
        branches = _gh_json(["api", f"repos/{repo}/branches", "--paginate"], timeout=20) or []
        for b in branches:
            name = b.get("name")
            if not name or name == default_branch or (repo, name) in seen:
                continue
            sha = (b.get("commit") or {}).get("sha")
            if not sha:
                continue
            commit = _gh_json(["api", f"repos/{repo}/commits/{sha}"], timeout=10) or {}
            author_login = (commit.get("author") or {}).get("login")
            if author_login != user:
                continue
            c = commit.get("commit") or {}
            rows.append({
                "kind": "branch", "repo": repo, "number": None, "branch": name,
                "title": name, "url": f"https://github.com/{repo}/tree/{name}",
                "is_draft": False, "approval": "", "ci": "none", "ci_checks": [],
                "commit_ts": parse_ts((c.get("committer") or {}).get("date")),
                "commit_sha": sha[:7],
                "commit_msg": (c.get("message") or "").splitlines()[0] if c.get("message") else "",
                "comment_ts": None, "comment_author": "", "comment_preview": "",
                "comment_full": "",
            })
    return rows, None


def kick_collect_prs():
    """Fire a non-blocking background collect_prs() if one isn't already
    running — same one-in-flight pattern as kick_collect()."""
    if not _pr_collect_inflight.acquire(blocking=False):
        return

    def run():
        try:
            rows, err = collect_prs()
            _pr_collect_result["prs"] = (rows, err)
            _pr_collect_result["prs_ts"] = datetime.now(timezone.utc)
        except Exception:
            log.exception("kick_collect_prs: scan failed")
        finally:
            _pr_collect_inflight.release()
    threading.Thread(target=run, daemon=True).start()


def kick_pr_action(kind, args, row_key):
    """Fire a mutating `gh` command (merge/close/ready-toggle/branch-delete)
    in the background. run_live polls _pr_action and shows the Cylon bar
    while running is True, then re-kicks collect_prs on completion."""
    if _pr_action["running"]:
        return False
    _pr_action.update(running=True, kind=kind, row_key=row_key,
                      started=time.monotonic(), error=None)

    def run():
        try:
            out = subprocess.run([_GH_BIN] + args, capture_output=True, text=True,
                                 timeout=30)
            if out.returncode != 0:
                _pr_action["error"] = out.stderr.strip()[:300] or "gh command failed"
        except (subprocess.SubprocessError, OSError) as e:
            _pr_action["error"] = str(e)
        finally:
            _pr_action["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def pr_action_args(kind, row):
    """Build the `gh` argv for a confirmed action on `row`."""
    repo, num, branch = row["repo"], row["number"], row["branch"]
    if kind == "merge":
        return ["pr", "merge", str(num), "--repo", repo, "--squash", "--delete-branch"]
    if kind == "close":
        return ["pr", "close", str(num), "--repo", repo]
    if kind == "ready":
        return ["pr", "ready", str(num), "--repo", repo]
    if kind == "draft":
        return ["pr", "ready", str(num), "--repo", repo, "--undo"]
    if kind == "delete":
        return ["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"]
    raise ValueError(kind)


def _token_stale():
    """True when the last usage fetch failed because the OAuth token is missing
    or rejected (401) — the only error `claude auth login` can fix."""
    err = _usage.get("err") or ""
    return err == "no oauth token" or err.startswith("HTTP 401")


# ── multi-account store (~/.claude/dashboard-accounts/<slug>.json) ───────────
# Each file snapshots the FULL ~/.claude/.credentials.json content (not just
# claudeAiOauth) so MCP-server tokens travel with the account too, plus a
# "label" (account email) for display. Switching just swaps this whole file
# into CREDS_PATH — Claude Code and this dashboard both re-read it fresh.

def _slugify(label):
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "account"


def _expiry_label(creds, now=None):
    """'expired' or 'expires HH:MM' (local time) from a credentials dict's
    claudeAiOauth.expiresAt (epoch ms). '' if the field is missing/invalid —
    Claude Code refreshes this token itself, so a saved snapshot's expiry is
    just informational, not a sign the account needs re-login."""
    ms = (creds.get("claudeAiOauth") or {}).get("expiresAt")
    if not isinstance(ms, (int, float)):
        return ""
    now = now or datetime.now(timezone.utc)
    exp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if exp <= now:
        return "expired"
    return "expires " + exp.astimezone().strftime("%H:%M")


def list_saved_accounts():
    """Return [(slug, label, expiry_label), ...] sorted by label. Corrupt
    files are skipped."""
    out = []
    for path in sorted(glob.glob(os.path.join(ACCOUNTS_DIR, "*.json"))):
        try:
            data = json.load(open(path))
            slug = os.path.splitext(os.path.basename(path))[0]
            out.append((slug, data.get("label") or slug,
                        _expiry_label(data.get("creds") or {})))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda t: t[1].lower())


def _same_account(creds_a, creds_b):
    """True if two full-credentials-file dicts are the same account. Compares
    refreshToken (stable across accessToken rotation) when both have one,
    else falls back to exact equality."""
    ra = (creds_a.get("claudeAiOauth") or {}).get("refreshToken")
    rb = (creds_b.get("claudeAiOauth") or {}).get("refreshToken")
    if ra and rb:
        return ra == rb
    return creds_a == creds_b


def current_account_slug():
    """Slug of the saved account matching the live creds file, or None if the
    live account was never snapshotted."""
    try:
        live = json.load(open(CREDS_PATH))
    except (OSError, ValueError):
        return None
    for path in sorted(glob.glob(os.path.join(ACCOUNTS_DIR, "*.json"))):
        try:
            creds = json.load(open(path)).get("creds") or {}
        except (OSError, ValueError):
            continue
        if _same_account(live, creds):
            return os.path.splitext(os.path.basename(path))[0]
    return None


def save_account_snapshot(label=None):
    """Snapshot the CURRENT live creds file into the accounts dir, keyed by
    account email (fetched fresh if not given). Skips the write if a saved
    account already holds byte-identical creds. Best-effort: returns the
    label used, or None on any failure (missing creds file, dead token,
    unreachable profile endpoint)."""
    try:
        raw = open(CREDS_PATH).read()
        creds = json.loads(raw)
    except (OSError, ValueError):
        return None
    if label is None:
        tok = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if not tok:
            return None
        try:
            req = urllib.request.Request(PROFILE_URL, headers=_oauth_headers(tok))
            with urllib.request.urlopen(req, timeout=10) as r:
                acct = (json.load(r).get("account") or {})
            label = acct.get("email") or acct.get("display_name")
        except Exception:
            return None
    if not label:
        return None
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    path = os.path.join(ACCOUNTS_DIR, _slugify(label) + ".json")
    if os.path.exists(path):
        try:
            existing = json.load(open(path))
            if existing.get("creds") == creds:
                return label          # already saved, byte-identical
        except (OSError, ValueError):
            pass
    json.dump({"label": label, "creds": creds}, open(path, "w"))
    return label


def switch_account(slug):
    """Snapshot the current account (so it isn't lost), then overwrite the
    live creds file with the saved account's. Non-disruptive: Claude Code and
    this dashboard both re-read CREDS_PATH fresh, so other running sessions
    pick up the new account on their next call, no restart needed."""
    path = os.path.join(ACCOUNTS_DIR, slug + ".json")
    try:
        creds = json.load(open(path))["creds"]
    except (OSError, ValueError, KeyError):
        return False
    save_account_snapshot()          # best-effort; swallows its own failures
    json.dump(creds, open(CREDS_PATH, "w"))
    return True


CYLON_RED = (255, 30, 30)
CYLON_TAIL = 4                 # fading trail length behind the eye
CYLON_PERIOD = 2.0             # seconds for one full sweep (both directions equal speed)
CYLON_FPS = 30                 # target redraw rate while any Cylon bar is on screen
CYLON_TICK = 1.0 / CYLON_FPS


def _cylon_bar(elapsed, width, tail=CYLON_TAIL, period=CYLON_PERIOD):
    """A Battlestar Galactica-style scanner: black background, one bright red
    'eye' cell, a fading tail behind it (opposite its direction of travel),
    ping-ponging across `width` cells at constant speed. Position is a pure
    function of elapsed time, not accumulated per-tick state, so it stays
    smooth regardless of render cadence."""
    t = elapsed % period
    half = period / 2
    frac = t / half if t < half else 2 - t / half     # 0 -> 1 -> 0, constant slope
    eye = round(frac * (width - 1))
    direction = 1 if t < half else -1                  # +1 = moving right
    cells = []
    for i in range(width):
        if i == eye:
            cells.append(styled(" ", (255, 255, 255), bg=CYLON_RED, bold=True))
            continue
        behind = (eye - i) if direction == 1 else (i - eye)
        if 0 < behind <= tail:
            c = shade(CYLON_RED, 1 - behind / (tail + 1))
            cells.append(styled(" ", c, bg=c))
        else:
            cells.append(styled(" ", (0, 0, 0), bg=(0, 0, 0)))
    return "".join(cells)


def _cylon_wait(proc, interval=CYLON_TICK, width=20):
    """Draw the Cylon bar on the terminal's last row while proc runs (used
    only by the suspended-terminal fallback, which has no TUI panel to draw
    into). `claude auth login` prints its own prompt once, then goes quiet
    during the browser/OAuth round trip — this bar is the only sign the click
    did anything during that gap."""
    try:
        rows = os.get_terminal_size().lines
    except OSError:
        proc.wait()
        return
    t0 = time.monotonic()
    try:
        while proc.poll() is None:
            bar = _cylon_bar(time.monotonic() - t0, width)
            sys.stdout.write(f"\0337\033[{rows};1H\033[K{bar} logging in...\0338")
            sys.stdout.flush()
            time.sleep(interval)
    finally:
        proc.wait()
        sys.stdout.write(f"\0337\033[{rows};1H\033[K\0338")
        sys.stdout.flush()


def _run_login_suspended(fd, old_term):
    """Suspend the TUI, run interactive `claude auth login`, then resume.

    Mirrors main()'s terminal enter/exit (alt-screen, SGR mouse, cbreak) so the
    login flow gets a normal cooked tty and the dashboard comes back exactly as
    it was. Resume is in a `finally` so a crash in login can't strand the
    terminal in raw/alt-screen state. fetch_usage reads the creds file fresh on
    every call, so the new token is picked up with no restart."""
    try:
        # --- suspend: undo main()'s terminal setup ---
        if old_term is not None:
            sys.stdout.write("\033[?1000l\033[?1003l\033[?1006l")          # mouse off
        sys.stdout.write("\033[?7h\033[?25h\033[?1049l")         # wrap+cursor on, leave alt
        sys.stdout.flush()
        if old_term is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)   # cooked mode
            except (termios.error, ValueError, OSError):
                pass
        try:
            proc = subprocess.Popen(["claude", "auth", "login"])
            _cylon_wait(proc)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("_run_login_suspended: claude auth login failed: %s", e)
    finally:
        # --- resume: redo main()'s terminal setup ---
        sys.stdout.write("\033[?1049h\033[?25l\033[?7l")         # alt, hide cursor, no wrap
        if old_term is not None:
            try:
                sys.stdout.write("\033[?1000h\033[?1003h\033[?1006h")       # mouse on
                tty.setcbreak(fd)
            except (termios.error, ValueError, OSError):
                pass
        sys.stdout.flush()


def endstr(iso, now):
    """Reset time as 'ends HH:MM' (today) or 'ends Ddd HH:MM' (other day)."""
    t = parse_ts(iso)
    if not t:
        return ""
    lt = t.astimezone()
    if lt.date() == now.astimezone().date():
        return "ends " + lt.strftime("%H:%M")
    return "ends " + lt.strftime("%a %H:%M")


def gauge_grade(pct):
    """Allowance gauge tiers: (colour, flashing). green<=70 · yellow<=80 ·
    amber<=90 · red<=95 · flashing red >95."""
    if pct > 95:
        return HOT_C, True
    if pct > 90:
        return HOT_C, False
    if pct > 80:
        return ORANGE_C, False
    if pct > 70:
        return WARN_C, False
    return OK_C, False


def vgauge(label, pct, end, inner, now):
    """A compact vertical-stack gauge: label / bar+pct / reset-time (3 rows).
    The bar flashes (1s on / 1s off) when over 95%."""
    pct = max(0.0, min(float(pct), 100.0))
    c, flashing = gauge_grade(pct)
    if flashing and int(now.timestamp()) % 2:      # off half of the 2s period
        c = shade(HOT_C, 0.22)
    barw = inner - 6
    fill = round(pct / 100 * barw)
    return [
        rgb(TEXT, label[:inner]),
        rgb(c, "█" * fill) + rgb(DIM2, "░" * (barw - fill)) + " "
        + rgb(c, f"{pct:>3.0f}%", bold=True),
        rgb(DIM, end[:inner]),
    ]


def retry_str(now):
    """Countdown to the next allowed fetch, e.g. 'retry in 8:32', else ''."""
    ra = _usage.get("retry_at")
    if not ra:
        return ""
    secs = int((ra - now).total_seconds())
    if secs <= 0:
        return "retrying…"
    return f"retry in {secs // 60}:{secs % 60:02d}"


def allowance_rows(now, anim, inner, lean=False):
    u = _usage
    err = u.get("err")
    rc = retry_str(now)
    if not u.get("data"):
        if err and err != "loading…":
            # Never succeeded and currently erroring: show the error + countdown.
            lines = [rgb(ACCENT, "live /usage", bold=True), rgb(HOT_C, err[:inner])]
            if rc:
                lines.append(rgb(WARN_C, rc[:inner]))
            return lines
        dots = "." * (anim % 3 + 1)
        return [rgb(ACCENT, "live /usage", bold=True), rgb(DIM, "loading" + dots), ""]
    d = u["data"]
    byk = {l.get("kind"): l for l in d.get("limits", [])}
    rows = []
    kinds = (("session", "5-hour session"),)
    if not lean:                 # lean tier drops the weekly gauge to save rows
        kinds += (("weekly_all", "weekly"),)
    for kind, label in kinds:
        lim = byk.get(kind)
        if not lim:
            continue
        rows += vgauge(label, lim.get("percent", 0),
                       endstr(lim.get("resets_at"), now), inner, now)
        rows.append("")
    if rows and rows[-1] == "":
        rows.pop()
    # Showing last-good gauges but a refresh is currently failing: flag it +
    # count down to the retry so the staleness is explicit, not silent.
    if err and rc:
        rows += ["", rgb(HOT_C, ("⚠ " + err)[:inner]), rgb(WARN_C, rc[:inner])]
    return rows


# ── frame ────────────────────────────────────────────────────────────────────

def render_too_small(cols, rows, need_rows):
    """A centred notice for terminals too small to fit the frame. Below this size
    the charts wrap and (worse) click hit-regions land on the wrong row, so we
    show this instead and the caller suppresses hits."""
    msg = [
        rgb(HOT_C, "terminal too small", bold=True),
        "",
        rgb(TEXT, f"need ≥ {TOTAL_WIDTH} cols × {need_rows} rows"),
        rgb(DIM, f"have {cols} × {rows}"),
        "",
        rgb(DIM, "resize the window  ·  ⌃C to quit"),
    ]
    pad_top = max((rows - len(msg)) // 2, 0)
    out = [""] * pad_top
    for line in msg:
        gap = max((cols - _visible_len(line)) // 2, 0)
        out.append(" " * gap + line)
    return "\n".join(out)


def heat_color(frac):
    """Colour ramp for the activity heatmap: cold grey -> blue -> green ->
    yellow -> red as `frac` goes 0..1."""
    stops = [(0.0, DIM2), (0.12, CO["uncached"]), (0.4, OK_C),
             (0.7, WARN_C), (1.0, HOT_C)]
    frac = max(0.0, min(1.0, frac))
    for i in range(len(stops) - 1):
        f0, c0 = stops[i]
        f1, c1 = stops[i + 1]
        if frac <= f1:
            t = 0 if f1 == f0 else (frac - f0) / (f1 - f0)
            return (int(lerp(c0[0], c1[0], t)), int(lerp(c0[1], c1[1], t)),
                    int(lerp(c0[2], c1[2], t)))
    return stops[-1][1]


def heatmap_content(heat):
    """Build the activity-heatmap panel body (no title/border): an hour header,
    7 weekday rows of 2-col cells coloured less->more by effective tokens, a
    per-day total column, and an intensity ramp. Returns (rows, inner) for use
    in panel() — both inline (beside the history SUMMARY) and as the M popup."""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    maxv = max((v for r in (heat or []) for v in r), default=0.0)
    if not heat or maxv <= 0:
        rows = [rgb(DIM, "no activity in the window yet")]
        return rows, max(_visible_len(r) for r in rows)
    CW = 2                                      # each hour cell is 2 cols wide
    # Hour header: even hours labelled, aligned to the cell's left column. The
    # 5-col prefix matches the "Mon  " day-label width below.
    hdr = "".join(f"{h:02d}" if h % 2 == 0 else " " * CW for h in range(24))
    rows = [rgb(DIM, " " * 5 + hdr + "  total")]
    for d in range(7):
        cells = []
        for h in range(24):
            v = heat[d][h]
            cells.append(rgb(DIM2, "·" * CW) if v <= 0
                         else rgb(heat_color(v / maxv), "█" * CW))
        tot = sum(heat[d])
        tot_s = rgb(TEXT, fmt_compact(round(tot))) if tot > 0 else rgb(DIM, "·")
        rows.append(rgb(TEXT, f"{days[d]:<4} ") + "".join(cells) + "  " + tot_s)
    ramp = "".join(rgb(heat_color(i / 8), "█") for i in range(9))
    rows += ["", rgb(DIM, "less ") + ramp + rgb(DIM, " more   weekday × local hour")]
    return rows, max(_visible_len(r) for r in rows)


def heatmap_panel(heat):
    """The activity heatmap as a titled panel (returns (lines, inner))."""
    rows, inner = heatmap_content(heat)
    return panel("ACTIVITY · weekday × hour", rows, inner), inner


CHARTS = [
    ("Input tokens · cache write disposition", "Input", ["uncached", "c5m", "c1h"],
     [("uncached", "uncached"), ("c5m", "5m · subagent"), ("c1h", "1h · main")]),
    ("Context assembly", "Context", ["read", "new", "miss"],
     [("read", "from cache"), ("new", "new input"), ("miss", "cache miss")]),
    ("Output tokens generated", "Output", ["output"],
     [("output", "output tokens")]),
]


def chart_block(buckets, height, compact, axes, blanks, now, anim=0,
                model_chart=False):
    """Render the three stacked charts with shared compaction so the main view
    and the session popup degrade identically: `compact` folds each chart's
    title+legend onto one line and drops the tick row, `axes` keeps the baseline,
    `blanks` keeps the blank line between charts. Returns (lines, bar_idx) where
    bar_idx are 0-based indices into `lines` that sit over the bars (clickable).
    `model_chart` appends a 4th chart stacking effective tokens by model (history
    view; requires buckets carrying the "models" key from collect(track_models))."""
    out, bar_idx = [], []
    for i, (title, short, keys, leg) in enumerate(CHARTS):
        if i and blanks:
            out.append("")
        if not compact:
            out.append("  " + legend(leg))
        start = len(out)
        out.extend(render_chart(title, keys, buckets, height, now, anim,
                                short_title=short, legend_items=leg,
                                compact=compact, axes=axes))
        bar_idx.extend(range(start + 1, start + 1 + height))   # bars follow header

    if model_chart:
        totals = {}
        for b in buckets:
            for mdl, v in b.get("models", {}).items():
                totals[mdl] = totals.get(mdl, 0) + v
        order = sorted(totals, key=lambda mdl: totals[mdl], reverse=True)
        cmap = {mdl: model_color(mdl) for mdl in order}

        def series_of(b):
            mm = b.get("models", {})
            return [(cmap[mdl], mm.get(mdl, 0)) for mdl in order]

        leg_str = ("   ".join(rgb(cmap[mdl], CHIP) + " " + rgb(DIM, mdl)
                              for mdl in order[:6])
                   or rgb(DIM, "no model data"))
        if blanks:
            out.append("")
        if not compact:
            out.append("  " + leg_str)
        start = len(out)
        out.extend(render_chart("Model mix · effective tokens", [], buckets,
                                height, now, anim, short_title="Models",
                                compact=compact, axes=axes,
                                series_of=series_of, legend_str=leg_str))
        bar_idx.extend(range(start + 1, start + 1 + height))
    return out, bar_idx


def chart_compaction(rows):
    """The compaction flags for a given terminal height, shared by the main view
    and the popup: same breakpoints as plan_layout."""
    return {"compact": rows < 39, "axes": rows >= 24, "blanks": rows >= 24}


def summary_panel(buckets, summary_tab, inner, lean=False, compact_nums=False):
    """Build the SUMMARY panel (tabbed) and its tab segments — shared by the
    inline layout and the key-opened popup."""
    if summary_tab == "aw":
        n = max(1, round(ACTIVE_WINDOW / BUCKET))
        s_buckets, win_label = buckets[-n:], fmt_window(ACTIVE_WINDOW)
    else:
        s_buckets, win_label = buckets, fmt_window(WINDOW)
    title, tlen, segs = summary_title(summary_tab)
    body = summary_rows(s_buckets, inner, win_label, lean=lean,
                        compact_nums=compact_nums)
    return panel(title, body, inner, title_len=tlen), segs


def render_frame(now, buckets, sessions, anim=0, layout=None, summary_tab="win",
                 cols=None, rows=None, mode="live"):
    """Return (frame_str, hits). hits maps clickable regions to TOKENS:
    [(term_row, x_lo, x_hi, token), ...] in 1-based terminal coordinates. A token
    is a session sid, a SUMMARY tab (TAB_WIN/TAB_AW), "__chart__", "__usage__",
    "__login__" (run the login flow), "__history__"/"__live__" (select that view),
    or "__exit__" (quit). `layout` is a
    plan_layout() dict; None means the full layout. `mode` is "live" or
    "history" — history uses a longer span, day-axis, and a single summary."""
    global VIEW_WINDOW, VIEW_BUCKET, VIEW_DAILY
    if mode == "history":
        VIEW_WINDOW, VIEW_BUCKET, VIEW_DAILY = HIST_WINDOW, HIST_BUCKET, True
    else:
        VIEW_WINDOW, VIEW_BUCKET, VIEW_DAILY = WINDOW, BUCKET, False
    if layout is None:
        layout = {"page_title": True, "footer": True, "compact": False,
                  "axes": True, "chart_blanks": True, "panels_lean": False,
                  "panels_inline": True, "sess_cols": ["c1h", "c12h", "ctx"],
                  "panel_cfg": {"summ_inner": SUMM_FULL,
                                "sess_cols": ["c1h", "c12h", "ctx"],
                                "ident": IDENT_MAX, "allow_inner": ALLOW_MAX,
                                "compact_nums": False},
                  "height": CHART_HEIGHT}
    height = layout["height"]
    compact, axes = layout["compact"], layout["axes"]
    out, hits = [], []

    if layout["page_title"]:
        local = now.astimezone()
        brand = rgb(ACCENT, "◆ ", bold=True) + rgb(TEXT, "CLAUDE CODE", bold=True)
        brand_len = 2 + len("CLAUDE CODE")
        tabs_str, tabs_len, segs = view_tabs(mode)
        ctx = ("HISTORY · last " + fmt_window(HIST_WINDOW)
               if mode == "history" else "live")
        clock = f"{local:%a %d %b · %H:%M:%S %Z}"
        # Signed-in account, far right and clickable (→ login, for switching
        # accounts). Re-read every refresh cycle by fetch_usage, so a login from
        # another terminal shows up here within one cycle. "⬢ " marks it.
        acct = _usage.get("account")
        acct_disp = ("⬢ " + acct) if acct else ""
        right_dim = ctx + "   " + clock
        right = rgb(DIM, right_dim)
        if acct_disp:
            right += rgb(DIM, "   ") + rgb(ACCENT, acct_disp, bold=True)
        right_len = _visible_len(right_dim) + (3 + _visible_len(acct_disp) if acct_disp else 0)
        BRAND_GAP = 4
        pad = max(TOTAL_WIDTH - brand_len - BRAND_GAP - tabs_len - right_len - 2, 1)
        out += [
            " " + brand + " " * BRAND_GAP + tabs_str + " " * pad + right,
            grad_rule(TOTAL_WIDTH, ACCENT2, ACCENT),
            "",
        ]
        # Tab click targets. Line is " "(col1) + brand + BRAND_GAP spaces + tabs;
        # so the tab string starts at column 1+brand_len+BRAND_GAP+1 (1-based).
        tab_x0 = 1 + brand_len + BRAND_GAP
        title_row = len(out) - 2
        for tok, lo, hi in segs:
            hits.append((title_row, tab_x0 + lo + 1, tab_x0 + hi + 1, tok))
        # The account string ends the line; map its columns to a login click.
        if acct_disp:
            line_end = 1 + brand_len + BRAND_GAP + tabs_len + pad + right_len
            acct_w = _visible_len(acct_disp)
            hits.append((title_row, line_end - acct_w + 1, line_end, "__login__"))

    # Charts. Bar rows carry the "__chart__" hit token (process_input derives the
    # bucket from the click x). Shared with the popup via chart_block.
    base = len(out)
    block, bar_idx = chart_block(buckets, height, compact, axes,
                                 layout["chart_blanks"], now, anim,
                                 model_chart=(mode == "history"))
    out += block
    hits += [(base + ri + 1, MARGIN + 1, MARGIN + NUM_BUCKETS, "__chart__")
             for ri in bar_idx]

    if mode == "history":
        # History: SUMMARY ($ cost + cache-hit + cache-mix, scoped to the week)
        # and the ACTIVITY heatmap side by side. When they don't fit they move
        # to the S/M popups (history_panels == "popup") and nothing renders here.
        if layout.get("history_panels") == "inline":
            inner = layout["panel_cfg"]["summ_inner"]
            srows = summary_rows(buckets, inner, fmt_window(HIST_WINDOW),
                                 lean=layout["panels_lean"], show_cost=True)
            summ_panel = panel("SUMMARY · last " + fmt_window(HIST_WINDOW),
                               srows, inner)
            heat_panel, _ = heatmap_panel(HIST_HEAT)
            out.append("")
            out += hjoin(summ_panel, heat_panel, gap=GAP)
    elif layout["panels_inline"]:
        lean = layout["panels_lean"]
        cfg = layout["panel_cfg"]
        sess_cols = cfg["sess_cols"]
        summ_inner, allow_inner = cfg["summ_inner"], cfg["allow_inner"]
        out.append("")
        summ, s_segs = summary_panel(buckets, summary_tab, summ_inner, lean=lean,
                                     compact_nums=cfg["compact_nums"])
        allow = panel("ALLOWANCE",
                      allowance_rows(now, int(now.timestamp()), allow_inner, lean=lean),
                      allow_inner)
        summ_total, allow_total = summ_inner + 2, allow_inner + 2

        panels, active_sids, sess_total = [summ], [], 0
        if sess_cols is not None:         # sessions panel sheds columns by width
            sess_inner = (SESS_FIXED_W + cfg["ident"]
                          + sum(SESS_COL_W[c] for c in sess_cols))
            sess_rows, active_sids = session_rows(sessions, now, sess_inner,
                                                  cols=tuple(sess_cols),
                                                  ident_w=cfg["ident"])
            if lean:                      # A4: cap the active-session list to 3
                sess_rows, active_sids = sess_rows[:1 + 3], active_sids[:3]
            sess_total = sess_inner + 2
            panels.append(panel(f"ACTIVE SESSIONS · last {fmt_window(ACTIVE_WINDOW)}",
                                 sess_rows, sess_inner))
        panels.append(allow)

        panel_start = len(out)
        out += hjoin(*panels, gap=GAP)
        tab_row = panel_start + 1
        hits += [(tab_row, 4 + lo, 4 + hi, tok) for tok, lo, hi in s_segs]
        if sess_cols is not None:
            x_lo = summ_total + GAP + 1
            x_hi = summ_total + GAP + sess_total
            hits += [(panel_start + 2 + j + 1, x_lo, x_hi, sid)
                     for j, sid in enumerate(active_sids)]
            allow_x0 = summ_total + GAP + sess_total + GAP + 1
        else:
            allow_x0 = summ_total + GAP + 1
        if _usage.get("err") and _usage["err"] != "loading…":
            hits += [(panel_start + k + 1, allow_x0, allow_x0 + allow_total - 1,
                      "__usage__") for k in range(len(allow))]

    if layout["footer"]:
        if mode == "history":
            if layout.get("history_panels") == "inline":
                foot = (f"history · {fmt_window(HIST_BUCKET)} buckets   ·   "
                        f"click a bar   ·   L live   ·   G login   ·   ? help   ·   ⌃C to exit")
                spans = [("L live", "__live__"), ("G login", "__login__"),
                         ("⌃C to exit", "__exit__")]
            else:                          # panels didn't fit — offer the popups
                foot = (f"history · {fmt_window(HIST_BUCKET)} buckets   ·   "
                        f"S summary   ·   M heatmap   ·   L live   ·   G login   ·   ⌃C to exit")
                spans = [("S summary", "__hsummary__"), ("M heatmap", "__heatmap__"),
                         ("L live", "__live__"), ("G login", "__login__"),
                         ("⌃C to exit", "__exit__")]
        else:
            plan = " · ".join(p for p in (_usage.get("sub"), _usage.get("tier")) if p)
            stamp = _usage["at"].astimezone().strftime("%H:%M:%S") if _usage.get("at") else "—"
            if not layout["panels_inline"]:
                extra = "   ·   s/e/w panels"
            elif layout.get("sess_cols") is None:
                extra = "   ·   e sessions"
            else:
                extra = ""
            foot = (f"plan {plan or '?'}   ·   allowance live, updated {stamp}   ·   "
                    f"charts every {max(1, INTERVAL_SECONDS // 60)}m{extra}   ·   "
                    f"H history   ·   G login   ·   ? help   ·   ⌃C to exit")
            spans = [("H history", "__history__"), ("G login", "__login__"),
                     ("⌃C to exit", "__exit__")]
        foot = foot[:TOTAL_WIDTH - 2]          # clip so it never wraps/overflows
        out += ["", "  " + rgb(DIM, foot)]
        # Clickable footer spans (H toggles history, M toggles the heatmap,
        # "⌃C to exit" quits). Coords are 1-based; the line has a 2-col indent,
        # so a substring at plain index i sits at terminal column i+3. Skip any
        # clipped off the end.
        foot_row = len(out)
        for sub, tok in spans:
            i = foot.find(sub)
            if i >= 0:
                lo = i + 3
                hits.append((foot_row, lo, lo + _visible_len(sub) - 1, tok))
    return "\n".join(out), hits


PR_COL_W = {"repo": 20, "number": 6, "what": 26, "approval": 3, "ci": 3, "commit": 30, "comment": 24}
PR_COLS = ("repo", "number", "what", "approval", "ci", "commit", "comment")
PR_GRID_SEP = rgb(DIM2, " │ ")
PR_GRID_SEP_LEN = 3


def _pr_relts(ts, now):
    if ts is None:
        return "—"
    secs = max(0, (now - ts).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _pr_ci_cell(state):
    return {"green": rgb((90, 220, 130), "●"), "red": rgb((240, 90, 90), "●"),
           "pending": rgb(WARN_C, "●"), "none": rgb(DIM2, "·")}[state]


def _pr_approval_cell(label):
    """Approval column is a single dot: solid red = review required and not
    yet given, solid green = approved, hollow green = no review required."""
    return {"Approved": rgb((90, 220, 130), "●"),
           "Awaiting review": rgb((240, 90, 90), "●"),
           "Changes requested": rgb((240, 90, 90), "●"),
           "No review needed": rgb((90, 220, 130), "○")}.get(label, rgb(DIM2, "·"))


def _pr_hrule(col_x, w, inner):
    """Horizontal gridline for the PRS table: dashes with a '┼' wherever a
    vertical PR_GRID_SEP's '│' crosses it."""
    line = ["─"] * inner
    for k in PR_COLS:
        p = col_x[k] + w[k] + 1
        if 0 <= p < inner:
            line[p] = "┼"
    return rgb(DIM2, "".join(line))


def _clip_ellip(s, width):
    """_clip, but a truncated string ends in '…' instead of a hard cut."""
    if _visible_len(s) <= width:
        return s
    return _clip(s, max(width - 1, 0)) + rgb(DIM, "…")


def pr_row_buttons(row):
    """(label, action_kind) pairs shown for this row, in click order."""
    btns = []
    if row["kind"] == "pr":
        if (row["approval"] in ("Approved", "No review needed") and row["ci"] == "green"
                and not row["is_draft"]):
            btns.append(("Merge", "merge"))
        btns.append(("Draft" if not row["is_draft"] else "Ready",
                     "draft" if not row["is_draft"] else "ready"))
        btns.append(("Close", "close"))
    else:
        btns.append(("Delete", "delete"))
    return btns


def render_prs_frame(now, rows, err, cols, term_rows, loading=False, elapsed=0,
                     last_refresh=None):
    """PRS tab: your open PRs + branches you've contributed to with no open
    PR. Returns (frame_str, hits, tips) — same convention as render_frame()
    plus `tips`: [(screen_row, lo, hi, full_text)] for cells whose shown text
    is truncated, consumed by the hover-tooltip lookup in run_live. Every
    interactive cell/button carries a token consumed by process_input:
      "__pr_open__<i>"            click a row -> open its URL
      "__pr_ci__<i>"              click a red CI dot -> failing-checks popup
      "__pr_comment__<i>"         click a comment cell -> full text popup
      "__pr_confirm__<kind>__<i>" click a button -> confirm-then-run popup
    `loading` (true only before the first scan ever completes) swaps the
    panel body for a Cylon progress bar, same treatment as the initial LIVE
    transcript scan — later background refreshes stay silent.
    """
    out, hits, tips = [], [], []
    w = dict(PR_COL_W)
    if rows:
        # REPO expands to fit the longest name in view, capped at 20% of the
        # terminal width (never shrinks below the default column width).
        longest_repo = max(_visible_len(r["repo"]) for r in rows)
        repo_cap = max(int(cols * 0.20), w["repo"])
        w["repo"] = min(max(longest_repo, w["repo"]), repo_cap)
    brand = rgb(ACCENT, "◆ ", bold=True) + rgb(TEXT, "CLAUDE CODE", bold=True)
    brand_len = 2 + len("CLAUDE CODE")
    tabs_str, tabs_len, segs = view_tabs("prs")
    local = now.astimezone()
    right = rgb(DIM, f"{local:%a %d %b · %H:%M:%S %Z}")
    BRAND_GAP = 4
    inner = max(cols - 2, sum(w[k] for k in PR_COLS) + PR_GRID_SEP_LEN * len(PR_COLS) + 30)
    total_width = inner + 2
    pad = max(total_width - brand_len - BRAND_GAP - tabs_len - _visible_len(f"{local:%a %d %b · %H:%M:%S %Z}") - 2, 1)
    out += [" " + brand + " " * BRAND_GAP + tabs_str + " " * pad + right,
           grad_rule(total_width, ACCENT2, ACCENT), ""]
    tab_x0 = 1 + brand_len + BRAND_GAP
    title_row = len(out) - 2
    for tok, lo, hi in segs:
        hits.append((title_row, tab_x0 + lo + 1, tab_x0 + hi + 1, tok))

    if loading:
        content, _ = _progress_content(inner, elapsed, "Fetching PRs and branches from GitHub…")
        out += panel("PRS", content, inner)
    elif err:
        body = [rgb(WARN_C, err)]
        out += panel("PRS", body, inner)
    elif not rows:
        out += panel("PRS", [rgb(DIM, "no open PRs, no unopened branches you've touched")], inner)
    else:
        head = (_padcol(rgb(TEXT, "REPO", bold=True), w["repo"]) + PR_GRID_SEP
               + _padcol(rgb(TEXT, "#", bold=True), w["number"]) + PR_GRID_SEP
               + _padcol(rgb(TEXT, "WHAT", bold=True), w["what"]) + PR_GRID_SEP
               + _padcol(rgb(TEXT, "A", bold=True), w["approval"]) + PR_GRID_SEP
               + _padcol(rgb(TEXT, "CI", bold=True), w["ci"]) + PR_GRID_SEP
               + _padcol(rgb(TEXT, "COMMIT", bold=True), w["commit"]) + PR_GRID_SEP
               + _padcol(rgb(TEXT, "COMMENT", bold=True), w["comment"]) + PR_GRID_SEP
               + rgb(TEXT, "ACTIONS", bold=True))
        # col_x: 0-based start offset of each column's TEXT within a content row
        # (panel() prefixes each row with "│ ", so add +2 for screen columns).
        # Each column is followed by a PR_GRID_SEP-wide gridline, so every
        # later column's offset must account for the separators before it.
        col_x = {}
        pos = 0
        for k in PR_COLS:
            col_x[k] = pos
            pos += w[k] + PR_GRID_SEP_LEN
        col_x["actions"] = pos
        hrule = _pr_hrule(col_x, w, inner)
        body = [head, hrule]
        for i, row in enumerate(rows):
            what = (("[draft] " if row["is_draft"] else "") + row["title"]) if row["kind"] == "pr" else row["branch"]
            number = f"#{row['number']}" if row["kind"] == "pr" else "—"
            commit = f"{row['commit_sha']} {_pr_relts(row['commit_ts'], now)} {row['commit_msg']}" if row["commit_sha"] else "—"
            comment = (f"{_pr_relts(row['comment_ts'], now)} {row['comment_author']}: {row['comment_preview']}"
                      if row["comment_full"] else "—")
            line = (_padcol(rgb(TEXT, _clip_ellip(row["repo"], w["repo"] - 1)), w["repo"]) + PR_GRID_SEP
                   + _padcol(rgb(DIM, number), w["number"]) + PR_GRID_SEP
                   + _padcol(rgb(TEXT, _clip_ellip(what, w["what"] - 1)), w["what"]) + PR_GRID_SEP
                   + _padcol(_pr_approval_cell(row["approval"]), w["approval"]) + PR_GRID_SEP
                   + _padcol(_pr_ci_cell(row["ci"]), w["ci"]) + PR_GRID_SEP
                   + _padcol(rgb(DIM, _clip_ellip(commit, w["commit"] - 1)), w["commit"]) + PR_GRID_SEP
                   + _padcol(rgb(DIM, _clip_ellip(comment, w["comment"] - 1)), w["comment"]) + PR_GRID_SEP)
            btns = pr_row_buttons(row)
            actions = "  ".join(f"[{lab}]" for lab, _ in btns)
            line += rgb(ACCENT, actions)
            body.append(line)
            row_i = len(body) - 1   # index within `body`, before panel() adds title+border
            # panel() prefixes each content row with a "│" border, so screen
            # column = 2 + body-string index (col1=border, col2=first content char).
            row_w = col_x["actions"] - PR_GRID_SEP_LEN   # up to (not incl.) the last gridline
            # More specific spans (CI dot, comment cell) must precede the
            # whole-row "open" span in `hits` — process_input's next() takes
            # the FIRST match, and these sit inside the row span.
            if row["ci"] == "red":
                hits.append((row_i, 2 + col_x["ci"], 1 + col_x["ci"] + w["ci"], f"__pr_ci__{i}"))
            if row["comment_full"]:
                hits.append((row_i, 2 + col_x["comment"], 1 + col_x["comment"] + w["comment"], f"__pr_comment__{i}"))
            hits.append((row_i, 2, 1 + row_w, f"__pr_open__{i}"))
            for key, full in (("repo", row["repo"]), ("what", what),
                              ("commit", commit), ("comment", comment)):
                if _visible_len(full) > w[key] - 1:   # actually got clipped
                    hits_lo, hits_hi = 2 + col_x[key], 1 + col_x[key] + w[key]
                    tips.append((row_i, hits_lo, hits_hi, full))
            bpos = col_x["actions"]
            for lab, kind in btns:
                btxt = f"[{lab}]"
                hits.append((row_i, 2 + bpos, 1 + bpos + len(btxt), f"__pr_confirm__{kind}__{i}"))
                bpos += len(btxt) + 2
            if i != len(rows) - 1:
                body.append(hrule)
        refresh_label = f" · updated {_pr_relts(last_refresh, now)}" if last_refresh else ""
        panel_lines = panel(f"PRS · {len(rows)} rows{refresh_label}", body, inner)
        panel_start = len(out)
        out += panel_lines
        # Translate body-relative row indices to screen coords: panel() adds one
        # title-border line before body row 0, so body row r sits at out-line
        # panel_start + 1 + r (0-based) -> screen row panel_start + 2 + r.
        hits = hits[:len(segs)] + [(panel_start + 2 + r, lo, hi, tok) for r, lo, hi, tok in hits[len(segs):]]
        tips = [(panel_start + 2 + r, lo, hi, full) for r, lo, hi, full in tips]

    foot = ("click a row to open · click a red CI dot / a comment for detail · "
           "click a button to act · L live · H history · ? help · ⌃C to exit")
    foot = foot[:total_width - 2]
    out += ["", "  " + rgb(DIM, foot)]
    foot_row = len(out)
    for sub, tok in (("L live", "__live__"), ("H history", "__history__"), ("⌃C to exit", "__exit__")):
        j = foot.find(sub)
        if j >= 0:
            hits.append((foot_row, j + 3, j + 3 + _visible_len(sub) - 1, tok))
    return "\n".join(out), hits, tips


def render_pr_tooltip(text, max_width):
    """Small floating box for a truncated cell's full text, drawn near the
    mouse cursor — not a modal overlay, doesn't touch the click hit-map."""
    inner = min(max(_visible_len(text), 4), max_width)
    lines = _wrap(text, inner)
    inner = max((_visible_len(l) for l in lines), default=inner)
    top = rgb(DIM2, "╭" + "─" * (inner + 2) + "╮")
    bot = rgb(DIM2, "╰" + "─" * (inner + 2) + "╯")
    mid = [rgb(DIM2, "│ ") + _padcol(rgb(TEXT, l), inner) + rgb(DIM2, " │") for l in lines]
    return [top] + mid + [bot]


def render_pr_ci_popup(row, cols, term_rows):
    checks = row["ci_checks"] or [("(no check detail returned)", "")]
    inner = min(max(cols - 10, 40), 80)
    body = [rgb(TEXT, f"{name}", bold=True) + "  " + rgb((240, 90, 90) if concl in
           ("FAILURE", "failure", "ERROR", "error") else DIM, str(concl))
           for name, concl in checks]
    body += ["", rgb(DIM, "click outside / esc / q to close")]
    return panel(f"CI checks · {row['repo']} #{row['number']}", body, inner)


def render_pr_comment_popup(row, cols, term_rows):
    inner = min(max(cols - 10, 40), 90)
    lines = _wrap(row["comment_full"] or "(empty)", inner)
    body = [rgb(TEXT, f"{row['comment_author']} · {_pr_relts(row['comment_ts'], datetime.now(timezone.utc))}", bold=True), ""]
    body += [rgb(TEXT, l) for l in lines]
    body += ["", rgb(DIM, "click outside / esc / q to close")]
    return panel("Comment", body, inner)


def render_pr_confirm_popup(kind, row, cols, term_rows):
    verb = {"merge": "Squash-merge", "close": "Close", "ready": "Mark ready",
           "draft": "Convert to draft", "delete": "Delete branch"}[kind]
    what = f"#{row['number']} {row['title']}" if row["kind"] == "pr" else row["branch"]
    inner = min(max(cols - 20, 40), 70)
    body = ["", rgb(TEXT, f"{verb} — {row['repo']}", bold=True),
           rgb(TEXT, what), "",
           rgb(WARN_C, "[Y] confirm", bold=True) + "    " + rgb(DIM, "[N] / esc cancel"), ""]
    regions = [(2, 1, len("[Y] confirm"), "__pr_do_confirm__"),
              (2, len("[Y] confirm") + 5, len("[Y] confirm") + 5 + len("[N] / esc cancel") - 1, "__pr_do_cancel__")]
    return panel("Confirm", body, inner), regions


def render_pr_progress_popup(kind, elapsed, cols, term_rows):
    verb = {"merge": "Merging…", "close": "Closing…", "ready": "Marking ready…",
           "draft": "Converting to draft…", "delete": "Deleting branch…"}.get(kind, "Working…")
    inner = min(max(cols - 20, 40), 60)
    content, _ = _progress_content(inner, elapsed, verb)
    return panel("PRS", content, inner)


def render_pr_error_popup(err, cols, term_rows):
    inner = min(max(cols - 20, 40), 70)
    body = ["", rgb(WARN_C, "gh command failed", bold=True), ""] + _wrap(err, inner)
    body += ["", rgb(DIM, "click outside / esc / q to dismiss")]
    return panel("Error", body, inner)


def term_cols():
    """Terminal width, or a 12h-at-5m fallback (152) when it can't be probed
    (piped --once) so non-interactive output keeps the historical default."""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return MARGIN + 144


def compute_history_dims():
    """Resolve HIST_WINDOW / HIST_BUCKET / HIST_NUM_BUCKETS from HISTORY_HOURS /
    HISTORY_BUCKET_MIN and the current chart width (NUM_BUCKETS). History shares
    the live chart width; with --history-bucket-minutes unset the window is fixed
    at HISTORY_HOURS and the bucket = window/width (a week stays a week while the
    bucket scales to the terminal); set, the bucket is fixed and the window =
    bucket × width. Recomputed on resize (the width changed)."""
    global HIST_WINDOW, HIST_BUCKET, HIST_NUM_BUCKETS
    HIST_NUM_BUCKETS = NUM_BUCKETS
    if HISTORY_BUCKET_MIN:
        HIST_BUCKET = timedelta(minutes=HISTORY_BUCKET_MIN)
        HIST_WINDOW = HIST_BUCKET * NUM_BUCKETS
    else:
        HIST_WINDOW = timedelta(hours=HISTORY_HOURS)
        HIST_BUCKET = HIST_WINDOW / NUM_BUCKETS


def configure_dimensions(args, cols, fail):
    """Resolve BUCKET / WINDOW / NUM_BUCKETS / ACTIVE_WINDOW / TOTAL_WIDTH /
    INTERVAL_SECONDS from the CLI args and terminal width; `fail(msg)` reports a
    validation error and exits. NUM_BUCKETS is fixed HERE at startup — the chart
    is MARGIN + NUM_BUCKETS columns wide, so history is bounded by terminal
    width. --window-hours unset => fill the available width (wider terminal =
    more history); given => fixed and validated to fit."""
    global BUCKET, WINDOW, NUM_BUCKETS, ACTIVE_WINDOW, TOTAL_WIDTH, INTERVAL_SECONDS
    global AUTOFIT, HISTORY_HOURS, HISTORY_BUCKET_MIN, PRICE_PER_MTOK

    bucket_min, aw_hours = args.bucket_minutes, args.active_window_hours
    if bucket_min < 1:
        fail("--bucket-minutes must be >= 1")
    if aw_hours <= 0:
        fail("--active-window-hours must be > 0")

    AUTOFIT = args.window_hours is None
    # Reserve the rightmost terminal column (RIGHT_RESERVE): writing a glyph to
    # the last column is unreliable — the per-line clear in the paint loop erases
    # it on some terminals, dropping a panel's right border at exact-fit widths.
    # Keeping everything within cols-1 sidesteps it entirely.
    avail = max(cols - MARGIN - RIGHT_RESERVE, 1)   # bucket columns width allows
    if AUTOFIT:
        nb = max(avail, MIN_BUCKETS)        # fill the terminal (tracks resize)
    else:
        if args.window_hours <= 0:
            fail("--window-hours must be > 0")
        win_min = round(args.window_hours * 60)
        if win_min % bucket_min:
            fail(f"--window-hours*60 ({win_min}) must be divisible by "
                 f"--bucket-minutes ({bucket_min})")
        nb = win_min // bucket_min
        if nb > avail:
            fail(f"--window-hours {args.window_hours:g} needs {nb} bars "
                 f"({MARGIN + nb + RIGHT_RESERVE} cols) but the terminal is {cols} wide. Widen "
                 f"it, lower --window-hours, or raise --bucket-minutes.")
    win_min = nb * bucket_min

    if bucket_min > aw_hours * 60:
        fail(f"--bucket-minutes ({bucket_min}) must be <= "
             f"--active-window-hours*60 ({aw_hours * 60:g})")

    BUCKET = timedelta(minutes=bucket_min)
    WINDOW = timedelta(minutes=win_min)
    NUM_BUCKETS = nb
    TOTAL_WIDTH = MARGIN + NUM_BUCKETS
    ACTIVE_WINDOW = max(timedelta(hours=aw_hours), BUCKET)
    INTERVAL_SECONDS = args.interval if args.interval else bucket_min * 60
    args.interval = INTERVAL_SECONDS

    # History view config (validated; dims derived in compute_history_dims).
    if args.history_hours <= 0:
        fail("--history-hours must be > 0")
    if args.history_bucket_minutes is not None and args.history_bucket_minutes < 1:
        fail("--history-bucket-minutes must be >= 1")
    if args.price_per_mtok < 0:
        fail("--price-per-mtok must be >= 0")
    HISTORY_HOURS = args.history_hours
    HISTORY_BUCKET_MIN = args.history_bucket_minutes
    PRICE_PER_MTOK = args.price_per_mtok
    compute_history_dims()        # the run loop reads args.interval


def refit_width(cols):
    """Autofit only: resize the window to the current terminal width. Returns
    True if the bucket count changed, so the caller forces a re-collect (the
    per-session bucket arrays are sized to NUM_BUCKETS)."""
    global WINDOW, NUM_BUCKETS, TOTAL_WIDTH
    if not AUTOFIT:
        return False
    nb = max(cols - MARGIN - RIGHT_RESERVE, MIN_BUCKETS)
    if nb == NUM_BUCKETS:
        return False
    NUM_BUCKETS, WINDOW, TOTAL_WIDTH = nb, BUCKET * nb, MARGIN + nb
    compute_history_dims()          # history shares the chart width — re-derive
    return True


class _ArgumentParser(argparse.ArgumentParser):
    """One flag (optionally with a quoted value) per line; blank lines and
    '#' comments ignored. Used for RC_PATH via fromfile_prefix_chars."""
    def convert_arg_line_to_args(self, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return []
        return shlex.split(line)


def main():
    ap = _ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        fromfile_prefix_chars="@")
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--interval", type=int, default=None, metavar="SECONDS",
                    help="seconds between transcript refreshes "
                         "(default: one bucket)")
    ap.add_argument("--window-hours", type=float, default=None, metavar="HOURS",
                    help="chart history span. Unset: fill the terminal width "
                         "(wider terminal -> more history). Given: fixed, and "
                         "validated to fit the width.")
    ap.add_argument("--bucket-minutes", type=int, default=5, metavar="MINUTES",
                    help="width of one chart bar / bucket (default 5)")
    ap.add_argument("--active-window-hours", type=float, default=1.0,
                    metavar="HOURS",
                    help="how far back a session (and, in the detail popup, its "
                         "subagents, and the SUMMARY active-window tab) counts "
                         "as active (default 1)")
    ap.add_argument("--history-hours", type=float, default=168.0, metavar="HOURS",
                    help="span of the history view (H key / footer). Default 168 "
                         "(1 week). The bucket auto-scales to fill the width "
                         "unless --history-bucket-minutes is given.")
    ap.add_argument("--history-bucket-minutes", type=int, default=None,
                    metavar="MINUTES",
                    help="fix the history bucket width instead of auto-scaling; "
                         "the history span then becomes bucket × chart-width")
    ap.add_argument("--price-per-mtok", type=float, default=5.0, metavar="USD",
                    help="base input $/million-tokens for the history $ estimate "
                         "(default 5.0 = Opus 4.8 input); effective tokens are "
                         "priced at this rate")
    ap.add_argument("--pr-refresh-seconds", type=int, default=300, metavar="SECONDS",
                    help="seconds between PRS-tab refreshes (default 300). Each "
                         "scan is several `gh` subprocess calls, so this is "
                         "deliberately coarser than --interval.")
    ap.add_argument("--exclude", default=None, metavar="PATH" + os.pathsep + "PATH",
                    help="full or partial cwd path(s) to leave out of every "
                         "chart/panel entirely, e.g. a background job's fixed "
                         "working directory. Case-insensitive substring match. "
                         f"{os.pathsep!r}-delimited (Python's os.pathsep on this "
                         "host: ';' on Windows, ':' on POSIX). No default - "
                         "nothing is excluded unless given.")
    argv = (["@" + RC_PATH] if os.path.isfile(RC_PATH) else []) + sys.argv[1:]
    args = ap.parse_args(argv)

    if args.exclude:
        EXCLUDE_PATTERNS.extend(
            p.strip().lower().replace("\\", "/")
            for p in args.exclude.split(os.pathsep) if p.strip())

    global PR_REFRESH_SECONDS
    PR_REFRESH_SECONDS = args.pr_refresh_seconds

    cols = term_cols()
    configure_dimensions(args, cols, ap.error)

    if args.once:
        now = datetime.now(timezone.utc)
        fetch_usage()                       # synchronous: single frame needs it
        buckets, sessions = collect(now)
        layout = None                       # full layout by default
        rows = None
        if sys.stdout.isatty():
            try:
                cols, rows = os.get_terminal_size()
                layout = plan_layout(rows, cols, sessions, now)
            except OSError:
                pass
        frame, _hits = render_frame(now, buckets, sessions, layout=layout,
                                    cols=cols, rows=rows)
        print(frame)
        return

    run_live(args)


GAP = 3
# SUMM_FULL is the non-lean floor: it must hold the widest cache-mix meter row
# ("▆ 5m cache · subagent" + pct = 28). SUMM_MIN is the lean floor (no meters,
# compact numbers). The summary may only shrink below SUMM_FULL when lean.
SUMM_FULL, SUMM_MIN = 28, 16
ALLOW_MAX, ALLOW_MIN = 26, 14                   # allowance inner range


def _panel_row_w(summ_inner, sess_cols, ident_w, allow_inner):
    """Total inline width of the panel row for a panel config."""
    row = (summ_inner + 2) + GAP + (allow_inner + 2)
    if sess_cols is not None:
        sess_inner = SESS_FIXED_W + ident_w + sum(SESS_COL_W[c] for c in sess_cols)
        row += GAP + (sess_inner + 2)
    return row


def fit_panels(cols, lean):
    """Pick the richest inline panel config that fits `cols`, or None (all
    panels move to the s/e/w popups). To keep the ACTIVE SESSIONS panel with its
    context column alive on a quarter-screen, it first sheds its 12h then 1h
    columns, then progressively (1) truncates the session name, (2) compresses
    the allowance panel, (3) — only when lean, since the cache-mix meters are
    then hidden — renders the summary numbers compactly. `lean` mirrors
    plan_layout: when False the summary shows the meters and is pinned to
    SUMM_FULL. Each candidate is (summ, sess_cols, ident, allow, compact_nums)."""
    cands = []
    for sc in (["c1h", "c12h", "ctx"], ["c1h", "ctx"], ["ctx"]):
        cands.append((SUMM_FULL, sc, IDENT_MAX, ALLOW_MAX, False))
    sc = ["ctx"]                                  # keep sessions + context, shrink rest
    for ident in (28, 24, 20, 16, IDENT_MIN):     # lever 1: truncate name
        cands.append((SUMM_FULL, sc, ident, ALLOW_MAX, False))
    for allow in (22, 18, ALLOW_MIN):             # lever 2: compress allowance
        cands.append((SUMM_FULL, sc, IDENT_MIN, allow, False))
    if lean:                                      # lever 3: compact summary numbers
        for summ in (24, 20, SUMM_MIN):
            cands.append((summ, sc, IDENT_MIN, ALLOW_MIN, True))
    # Last resorts: drop the sessions panel, then (lean only) shrink the rest.
    cands.append((SUMM_FULL, None, 0, ALLOW_MAX, False))
    if lean:
        cands.append((SUMM_MIN, None, 0, ALLOW_MIN, True))
    for summ_inner, sess_cols, ident, allow, compact_nums in cands:
        if cols >= _panel_row_w(summ_inner, sess_cols, ident, allow):
            return {"summ_inner": summ_inner, "sess_cols": sess_cols,
                    "ident": ident, "allow_inner": allow,
                    "compact_nums": compact_nums}
    return None


def plan_layout(rows, cols, sessions, now, history=False):
    """Decide which elements render inline, degrading as the terminal shrinks,
    and pick the chart bar height to fill what's left. `history` plans the
    history view (one SUMMARY panel, no sessions/allowance).

    Height ladder: <39 fold each chart title+legend onto one line and drop the
    tick row; <34 drop the page title; <31 drop the footer; <29 lean the panels
    (no cache breakdown / weekly gauge, <=3 sessions); <24 drop chart baselines
    and inter-chart blanks; <15 drop inline panels.
    Width ladder: the sessions panel sheds columns (12h -> 1h -> context ->
    none) to keep fitting; below ~66 cols even summary+allowance move to the
    s/e/w popups."""
    cutoff = now - ACTIVE_WINDOW
    n_active = sum(1 for s in sessions.values() if s["last_act"] >= cutoff)
    lean = rows < 29
    L = {
        "page_title":   rows >= 34,
        "footer":       rows >= 31,
        "compact":      rows < 39,
        "axes":         rows >= 24,
        "chart_blanks": rows >= 24,
        "panels_lean":  lean,
    }
    per_chart = (1 if L["compact"] else 2)
    if L["axes"]:
        per_chart += (1 if L["compact"] else 2)
    base = 3 * per_chart
    base += 2 if L["chart_blanks"] else 0
    if L["page_title"]:
        base += 3
    if L["footer"]:
        base += 2

    if history:
        # 4 charts (3 standard + model-mix), then a panel row of SUMMARY (with $
        # cost + cache-hit) and the ACTIVITY heatmap side by side. If the width
        # can't hold both or the charts would drop below MIN_BAR_H, the panels
        # move to the S/M popups and the charts take the freed rows.
        ncharts = 4
        hbase = ncharts * per_chart + ((ncharts - 1) if L["chart_blanks"] else 0)
        if L["page_title"]:
            hbase += 3
        if L["footer"]:
            hbase += 2
        summ = (6 if lean else 11)          # summary_rows length incl. cost lines
        HEAT_ROWS, HEAT_INNER = 10, 61      # header+7 days+blank+ramp ; grid width
        panel_body = 1 + 2 + max(summ, HEAT_ROWS)   # blank + borders + tallest panel
        row_w = (SUMM_FULL + 2) + GAP + (HEAT_INNER + 2)
        inline = ((cols - RIGHT_RESERVE) >= row_w
                  and (rows - hbase - panel_body) // ncharts >= MIN_BAR_H)
        if not inline:
            panel_body = 0                  # popups instead — charts take the rows
        L["history_panels"] = "inline" if inline else "popup"
        L["panels_inline"] = inline
        L["panel_cfg"] = {"summ_inner": SUMM_FULL}
        L["sess_cols"] = None
        L["height"] = max(MIN_BAR_H,
                          min(CHART_HEIGHT, (rows - hbase - panel_body) // ncharts))
        return L

    # Panels go inline only if they fit the width AND still leave the charts at
    # the minimum bar height; otherwise they move to the s/e/w popups and the
    # charts take the freed rows. panel_body is the EXACT rendered height (the
    # tallest of the three panels) so the charts fill the rest with no waste.
    cfg = fit_panels(cols - RIGHT_RESERVE, lean)
    panel_body = 0
    if cfg is not None:
        summ = 4 if lean else 9             # summary_rows length
        if cfg["sess_cols"] is None:
            sess = 0
        elif n_active == 0:
            sess = 2                        # header + "no sessions" line
        else:
            sess = 1 + (min(n_active, 3) if lean else n_active)
        alw = len(allowance_rows(now, 0, cfg["allow_inner"], lean=lean))
        panel_body = 1 + 2 + max(summ, sess, alw)           # blank+borders+body
        if (rows - base - panel_body) // 3 < MIN_BAR_H:
            cfg, panel_body = None, 0
    L["panels_inline"] = cfg is not None
    L["panel_cfg"] = cfg
    L["sess_cols"] = cfg["sess_cols"] if cfg else None
    L["height"] = max(MIN_BAR_H, min(CHART_HEIGHT, (rows - base - panel_body) // 3))
    return L


def run_live(args):
    log.info("dashboard start: interval=%ss tick=%ss", args.interval, TICK_SECONDS)
    alt = sys.stdout.isatty()
    fd = sys.stdin.fileno() if alt else None
    old_term = None
    if alt:
        # Alt screen, hide cursor, and DISABLE autowrap (?7l): a glyph written
        # to the last terminal column otherwise leaves a pending wrap that some
        # terminals/tmux smear or drop — which dropped the rightmost panel's
        # border at widths where the panel row exactly filled the screen. With
        # autowrap off, full-width lines render in place (and any stray
        # over-width line clips instead of wrapping + desyncing the layout).
        sys.stdout.write("\033[?1049h\033[?25l\033[?7l")
        # Enable SGR mouse reporting + cbreak input so clicks/keys arrive
        # immediately. cbreak (not raw) keeps ISIG, so ⌃C still raises.
        try:
            sys.stdout.write("\033[?1000h\033[?1003h\033[?1006h")   # 1003: any-motion, for PRS-tab hover tooltips
            old_term = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            # NB: do NOT set stdin non-blocking. stdin/stdout share one tty file
            # description, so O_NONBLOCK on fd 0 also makes stdout non-blocking,
            # and a large frame write then raises BlockingIOError. select() below
            # gives readiness; os.read() after a positive select never blocks.
        except (termios.error, ValueError, OSError):
            old_term = None        # terminal doesn't support it; degrade to no mouse
    sys.stdout.flush()

    buckets, sessions = [empty_bucket() for _ in range(NUM_BUCKETS)], {}
    last_collect = last_usage = None
    # History view: a separate, on-demand scan over the longer HIST_WINDOW,
    # cached and refreshed on --interval (and on resize). Only run while the
    # history view is open — a week-wide transcript scan is heavy.
    hist_buckets, hist_sessions, last_hist_collect = [], {}, None
    show_history = False
    # PRS view: open PRs + unopened contributed branches via `gh`. Own refresh
    # cadence (PR_REFRESH_SECONDS, much slower than --interval — each scan is
    # several `gh` subprocess calls) and its own small overlay state, since its
    # popups (CI/comment drilldown, confirm-then-run) are independent of the
    # session/bucket/panel popups the live/history views use.
    show_prs = False
    pr_rows, pr_err, last_pr_collect = [], None, None
    pr_tips, pr_hover = [], None
    pr_ui = {"ci_idx": None, "comment_idx": None, "confirm": None, "err": None}
    pr_action_running_prev = False
    pr_action_started = None
    anim = 0
    anim_t0 = time.monotonic()   # shimmer phase is wallclock-derived, not tick-count —
                                  # see `anim` reassignment below (tick rate varies: 5fps
                                  # normal, 30fps while a Cylon bar is on screen)
    hits = []
    focus_sid = None
    focus_bucket = None
    panel_view = None            # None | "summary" | "sessions" | "allow"
    summary_tab = "win"
    show_help = False
    show_uerr = False
    show_login = False           # login confirmation modal
    login_proc = None            # Popen of an in-progress `claude auth login`
    login_started = None         # time.monotonic() it was started
    first_load_done = False      # gates the LOADING popup to the first-ever scan
    loading_started = time.monotonic()
    overlay_scroll = 0
    prev_okey = None
    mouse_re = re.compile(r"\033\[<(\d+);(\d+);(\d+)([Mm])")
    try:
        while True:
          # A transient bad frame (unexpected data shape, a render edge case on a
          # weird terminal) must not kill an hours-long monitor: log it and carry
          # on. KeyboardInterrupt is NOT caught here (it's BaseException) so ⌃C
          # still breaks to the finally that restores the terminal.
          try:
            now = datetime.now(timezone.utc)
            # Wallclock-derived, not incremented per tick: the loop's tick rate
            # varies (TICK_SECONDS normally, CYLON_TICK — 30fps — while a Cylon
            # bar is on screen), and a per-tick counter would make the shimmer
            # run 6x too fast during those windows. Units match a TICK_SECONDS
            # tick so shimmer speed at the normal cadence is unchanged.
            anim = int((time.monotonic() - anim_t0) / TICK_SECONDS)
            # Poll a background login every tick, whether or not new input
            # arrived, so the progress bar animates and completion is noticed
            # promptly. On timeout (an SSO variant needing keyboard input,
            # which hangs forever with stdin closed), fall back to a real
            # suspended terminal — that call blocks, same as before.
            if login_proc is not None:
                if login_proc.poll() is not None:
                    login_proc = None
                    save_account_snapshot()
                    kick_usage()
                    show_login = False
                    prev_okey = None
                elif time.monotonic() - login_started > LOGIN_INLINE_TIMEOUT:
                    login_proc.terminate()
                    try:
                        login_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        login_proc.kill()
                    login_proc = None
                    _run_login_suspended(fd, old_term)
                    save_account_snapshot()
                    kick_usage()
                    show_login = False
                    prev_okey = None
            try:
                cols, rows = os.get_terminal_size()
            except OSError:
                cols, rows = TOTAL_WIDTH, 50
            # Autofit: a width change resizes the window, which re-buckets — so
            # force a re-collect this tick (per-session bucket arrays are sized
            # to NUM_BUCKETS). Shrinking the terminal now just shows less history
            # instead of tripping the too-small notice.
            if refit_width(cols):
                last_collect = last_hist_collect = None   # width -> re-bucket both
                # Existing sessions' per-session bucket arrays are sized to the
                # OLD NUM_BUCKETS; seeding the re-collect with them would carry
                # forward mismatched-size arrays until each is re-touched. A
                # resize is rare and already a visible discontinuity, so just
                # drop them (and any in-flight scan's stale-sized publish, on
                # the rare chance one lands before the fresh kick below)
                # rather than add bucket-resizing logic for it.
                sessions, hist_sessions = {}, {}
                buckets, hist_buckets = [empty_bucket() for _ in range(NUM_BUCKETS)], []
                _collect_result.pop("live", None)
                _collect_result.pop("history", None)
            # Heavy transcript scan only every --interval; runs in the
            # background (kick_collect) so this loop's input-polling select()
            # below is never blocked by a slow scan — first load included.
            if last_collect is None or (now - last_collect).total_seconds() >= args.interval:
                kick_collect("live", now)
                last_collect = now
            buckets, sessions = _collect_result.get("live", (buckets, sessions))
            if not first_load_done and not _collect_inflight["live"].locked():
                first_load_done = True   # first scan ever finished: LOADING never shows again
            # Refresh allowance every USAGE_REFRESH; a failed fetch sets a
            # retry_at (USAGE_BACKOFF out), so a non-2xx makes us wait instead of
            # hammering. Honour that countdown before the normal cadence.
            ra = _usage.get("retry_at")
            if ra is not None and now < ra:
                due = False
            else:
                due = last_usage is None or (now - last_usage).total_seconds() >= USAGE_REFRESH
            if due:
                kick_usage()        # fetch_usage owns retry_at (sets/clears it)
                last_usage = now

            # History view: collect its (longer, coarser) buckets on demand —
            # on entry, on --interval, and after a resize. Select which dataset
            # and mode this tick renders.
            # Fast repaint every tick: animates loading, keeps the clock live,
            # and surfaces the background usage fetch within ~1s of completion.
            if show_history:
                if (last_hist_collect is None
                        or (now - last_hist_collect).total_seconds() >= args.interval):
                    kick_collect("history", now, HIST_WINDOW, HIST_BUCKET,
                                 HIST_NUM_BUCKETS, track_models=True,
                                 track_heatmap=True)
                    last_hist_collect = now
                hist_buckets, hist_sessions = _collect_result.get(
                    "history", (hist_buckets, hist_sessions))
                cur_buckets, cur_sessions = hist_buckets, hist_sessions
                layout = (plan_layout(rows, cols, hist_sessions, now,
                                      history=True) if alt else None)
                frame, hits = render_frame(now, hist_buckets, hist_sessions,
                                           anim, layout, summary_tab,
                                           cols=cols, rows=rows, mode="history")
            elif show_prs:
                if (last_pr_collect is None
                        or (now - last_pr_collect).total_seconds() >= PR_REFRESH_SECONDS):
                    kick_collect_prs()
                    last_pr_collect = now
                pr_rows, pr_err = _pr_collect_result.get("prs", (pr_rows, pr_err))
                # A just-finished action's row is stale until the next scan —
                # force one immediately rather than waiting PR_REFRESH_SECONDS.
                if pr_action_running_prev and not _pr_action["running"]:
                    last_pr_collect = None
                    if _pr_action["error"]:
                        pr_ui["err"] = _pr_action["error"]
                        _pr_action["error"] = None
                pr_action_running_prev = _pr_action["running"]
                cur_buckets, cur_sessions = buckets, sessions
                pr_loading = "prs" not in _pr_collect_result
                pr_elapsed = (now - last_pr_collect).total_seconds() if last_pr_collect else 0
                frame, hits, pr_tips = render_prs_frame(
                    now, pr_rows, pr_err, cols, rows, loading=pr_loading, elapsed=pr_elapsed,
                    last_refresh=_pr_collect_result.get("prs_ts"))
            else:
                cur_buckets, cur_sessions = buckets, sessions
                layout = plan_layout(rows, cols, sessions, now) if alt else None
                frame, hits = render_frame(now, buckets, sessions, anim, layout,
                                           summary_tab, cols=cols, rows=rows,
                                           mode="live")
            # Too small to fit? The frame would overflow and scroll, desyncing the
            # click hit-regions onto the wrong rows. Show a notice and drop hits so
            # clicks can't misfire; close any overlay until there's room again.
            if alt and (cols < TOTAL_WIDTH or rows < 9
                        or frame.count("\n") + 1 > rows):
                hits = []
                show_help = show_uerr = show_login = False
                panel_view = None
                focus_sid = focus_bucket = None
                pr_ui = {"ci_idx": None, "comment_idx": None, "confirm": None, "err": None}
                frame = render_too_small(cols, rows, 9)
            if alt:
                # One overlay at a time: loading > help > login-confirm >
                # usage-error > session > bucket > panel popup. overlay_regions
                # carries a modal popup's clickable spans (relative to the
                # box); translated below.
                overlay, okey, overlay_regions = None, None, []
                if not first_load_done:
                    overlay, overlay_regions = render_loading(
                        now, cols, rows, time.monotonic() - loading_started)
                    okey = "loading"
                elif show_help:
                    overlay = render_help(now, cols, rows)
                    okey = "help"
                elif show_login:
                    login_elapsed = (time.monotonic() - login_started
                                      if login_proc is not None else None)
                    overlay, overlay_regions = render_login_confirm(
                        now, cols, rows, login_elapsed)
                    okey = "login"
                elif show_uerr:
                    overlay = render_usage_error(now, cols, rows)
                    if overlay is None:
                        show_uerr = False
                    else:
                        okey = "uerr"
                elif focus_sid is not None:
                    overlay = render_popup(focus_sid, cur_sessions, now, cols, rows, anim)
                    if overlay is None:
                        focus_sid = None
                    else:
                        okey = ("popup", focus_sid)
                elif focus_bucket is not None:
                    overlay = render_bucket_popup(focus_bucket, cur_sessions, now, cols, rows)
                    if overlay is None:
                        focus_bucket = None
                    else:
                        okey = ("bucket", focus_bucket)
                elif panel_view is not None:
                    overlay, overlay_regions = render_panel_popup(
                        panel_view, cur_buckets, cur_sessions, now, rows, summary_tab)
                    if overlay is None:
                        panel_view = None
                    else:
                        okey = ("panel", panel_view, summary_tab)
                elif pr_ui["confirm"] is not None:
                    kind, idx = pr_ui["confirm"]
                    if idx < len(pr_rows):
                        overlay, overlay_regions = render_pr_confirm_popup(
                            kind, pr_rows[idx], cols, rows)
                        okey = ("prconfirm", kind, idx)
                    else:
                        pr_ui["confirm"] = None
                elif _pr_action["running"]:
                    elapsed = time.monotonic() - (pr_action_started or time.monotonic())
                    overlay = render_pr_progress_popup(_pr_action["kind"], elapsed, cols, rows)
                    okey = "praction"
                elif pr_ui["err"]:
                    overlay = render_pr_error_popup(pr_ui["err"], cols, rows)
                    okey = "prerr"
                elif pr_ui["ci_idx"] is not None:
                    if pr_ui["ci_idx"] < len(pr_rows):
                        overlay = render_pr_ci_popup(pr_rows[pr_ui["ci_idx"]], cols, rows)
                        okey = ("prci", pr_ui["ci_idx"])
                    else:
                        pr_ui["ci_idx"] = None
                elif pr_ui["comment_idx"] is not None:
                    if pr_ui["comment_idx"] < len(pr_rows):
                        overlay = render_pr_comment_popup(pr_rows[pr_ui["comment_idx"]], cols, rows)
                        okey = ("prcomment", pr_ui["comment_idx"])
                    else:
                        pr_ui["comment_idx"] = None
                if overlay is not None:
                    # Reset scroll on a fresh/changed overlay, then fit it to the
                    # terminal (clip width, scroll + scrollbar when too tall).
                    if okey != prev_okey:
                        overlay_scroll = 0
                    overlay, max_scroll = fit_overlay(overlay, cols, rows, overlay_scroll)
                    overlay_scroll = max(0, min(overlay_scroll, max_scroll))
                if overlay is None:
                    # No overlay: full base repaint each tick (shimmer live).
                    body = "\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J"
                    sys.stdout.write(body)
                    # PRS hover tooltip: floats near the cursor, doesn't touch
                    # `hits` — clicks still work normally while it's showing.
                    if show_prs and pr_hover is not None:
                        hx, hy = pr_hover
                        tip_text = next((full for (tr, lo, hi, full) in pr_tips
                                        if tr == hy and lo <= hx <= hi), None)
                        if tip_text is not None:
                            box = render_pr_tooltip(tip_text, min(cols - 4, 60))
                            bw = max((_visible_len(l) for l in box), default=0)
                            row0 = max(1, min(hy + 1, rows - len(box)))
                            col0 = max(1, min(hx, cols - bw + 1))
                            for k, ln in enumerate(box):
                                sys.stdout.write(f"\033[{row0 + k};{col0}H" + ln)
                else:
                    oh = len(overlay)
                    ow = max((_visible_len(x) for x in overlay), default=0)
                    row0 = max((rows - oh) // 2, 1)
                    col0 = max((cols - ow) // 2, 1)
                    # A modal panel popup is clickable: translate its box-relative
                    # regions to screen coords and make them THE hit map (base
                    # regions are hidden under the modal). Only when it fits
                    # un-scrolled — a scrolled view shifts the row indices, so we
                    # drop row clicks there (scroll/close still work).
                    hits = []
                    if overlay_regions and max_scroll == 0:
                        hits = [(row0 + li, col0 + lo, col0 + hi, tok)
                                for li, lo, hi, tok in overlay_regions]
                    # Paint the base ONCE when the overlay opens or switches, then
                    # only redraw the overlay box in place each tick — repainting
                    # the whole base every tick under the overlay used to flicker
                    # it (a base write covering the overlay's own rows, followed
                    # a moment later by the overlay redraw covering them again).
                    # Exception: "loading" repaints the base every tick regardless
                    # (the whole point is watching sessions/bars fill in live
                    # behind the popup) — but does it via _clip/_slice_from so
                    # the overlay's own rectangle is never in that base write at
                    # all, just the flanking columns; only the overlay-redraw
                    # loop below ever touches those cells, so there's nothing
                    # left to flicker.
                    if okey != prev_okey or okey == "loading":
                        r0, r1 = row0, row0 + oh - 1
                        c0, c1 = col0 - 1, col0 - 1 + ow   # overlay's visible-col span
                        parts = []
                        for li, ln in enumerate(frame.split("\n")):
                            r = li + 1
                            if r0 <= r <= r1:
                                parts.append(f"\033[{r};1H" + _clip(ln, c0)
                                             + f"\033[{r};{c1 + 1}H"
                                             + _slice_from(ln, c1) + "\033[K")
                            else:
                                parts.append(f"\033[{r};1H" + ln + "\033[K")
                        sys.stdout.write("".join(parts) + "\033[J")
                    for k, pl in enumerate(overlay):
                        sys.stdout.write(f"\033[{row0 + k};{col0}H" + _padcol(pl, ow))
                prev_okey = okey
            else:
                sys.stdout.write(frame + "\n")
            sys.stdout.flush()

            # Input-aware wait: wake early on a click/keypress for ≤1-tick latency.
            # A Cylon bar is on screen (LOADING or login popup) needs a
            # reliable 30fps redraw, well above the normal 5fps shimmer
            # cadence — narrow the wait just for that window.
            cylon_active = (login_proc is not None or not first_load_done
                            or _pr_action["running"]
                            or (show_prs and "prs" not in _pr_collect_result))
            if alt and old_term is not None:
                r, _, _ = select.select([sys.stdin], [], [],
                                        CYLON_TICK if cylon_active else TICK_SECONDS)
                if r:
                    try:
                        data = os.read(fd, 4096).decode("utf-8", "ignore")
                    except OSError:
                        data = ""
                    do_prs = False
                    if show_prs:
                        scroll_delta = 0
                        do_login = do_retry = do_switch = do_cancel = False
                        (pr_ui, show_help, go_live, go_history, quit_flag,
                         do_pr_run, pr_hover) = process_prs_input(
                            data, mouse_re, hits, pr_ui, pr_rows, show_help,
                            _pr_action["running"], pr_hover)
                        if go_live or go_history:
                            show_prs = False
                            show_history = go_history
                        if do_pr_run is not None:
                            kind, row = do_pr_run
                            if kick_pr_action(kind, pr_action_args(kind, row),
                                              (row["repo"], row["number"] or row["branch"])):
                                pr_action_started = time.monotonic()
                    else:
                        (focus_sid, focus_bucket, panel_view, summary_tab, show_help,
                         show_uerr, show_login, show_history, quit_flag,
                         scroll_delta, do_login, do_retry, do_switch,
                         do_cancel, do_prs) = process_input(
                            data, mouse_re, hits, focus_sid, focus_bucket,
                            panel_view, summary_tab, show_help, show_uerr, show_login,
                            show_history, login_proc is not None)
                        if do_prs:
                            show_prs, show_history = True, False
                            pr_ui = {"ci_idx": None, "comment_idx": None,
                                    "confirm": None, "err": None}
                    if quit_flag:              # footer "⌃C to exit" was clicked
                        break
                    # Manual retry/login bypass the retry_at backoff gate above.
                    if do_retry:
                        kick_usage()
                    if do_switch:              # picked a saved account: just a
                        switch_account(do_switch)   # file swap, no TUI suspend
                        kick_usage()
                    if do_login:               # start it; the per-tick poll
                        try:                    # above notices completion/timeout
                            login_proc = subprocess.Popen(
                                ["claude", "auth", "login"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
                            login_started = time.monotonic()
                        except (OSError, subprocess.SubprocessError) as e:
                            log.warning("main: claude auth login failed: %s", e)
                            login_proc = None
                            show_login = False
                    if do_cancel and login_proc is not None:
                        login_proc.terminate()
                        try:
                            login_proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            login_proc.kill()
                        login_proc = None
                    # Any open overlay scrolls; the delta is clamped to the
                    # overlay's range each render (and reset when it changes).
                    overlay_scroll += scroll_delta
            else:
                time.sleep(TICK_SECONDS)
          except Exception:
              log.exception("render loop: frame failed, continuing")
              time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        if alt:
            if old_term is not None:
                try:
                    sys.stdout.write("\033[?1000l\033[?1003l\033[?1006l")   # disable mouse
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
                except (termios.error, ValueError, OSError):
                    pass
            sys.stdout.write("\033[?7h\033[?25h\033[?1049l")   # re-enable wrap, show cursor, leave alt
            sys.stdout.flush()


def process_input(data, mouse_re, hits, focus_sid, focus_bucket, panel_view,
                  summary_tab, show_help, show_uerr, show_login, show_history,
                  login_active=False):
    # do_prs (returned) tells run_live to switch into the PRS view — that
    # view has its own overlay state and input handling (process_prs_input),
    # so this function only needs to recognise the tab click / 'P' key.
    """Update the overlay/selection state from a chunk of terminal input and
    return a scroll delta for the (only scrollable) help overlay.

    A left-click on a session row opens/switches its popup; a click on a chart
    bar (token "__chart__") drills into that bucket; a click on a SUMMARY tab
    (TAB_WIN/TAB_AW) switches the summary window; a click on the ALLOWANCE panel
    (token "__usage__") opens the usage-error overlay; a click outside closes
    whatever overlay is open. '?' toggles help; s/e/w toggle the SUMMARY /
    sessions / allowance popups (for layouts too small to show them inline);
    q/bare-esc steps back one overlay level. Mouse wheel and arrow/PgUp/PgDn/j/k
    scroll the help overlay.

    'H'/'L' (or a click on the Live/History menu-bar tabs, or the footer
    "H history"/"L live" span) select the history/live view; in history (when
    the panels don't fit inline) S / M open the SUMMARY / heatmap popups via
    panel_view ("hsummary"/"heatmap"); a click on the footer "⌃C to exit" span
    requests quit.

    'g'/'G', a click on the account string / footer "G login", or [L] in the
    usage-error overlay open the SWITCH ACCOUNT modal (show_login) — a table
    of saved accounts. There, a digit / a row's [Select] click picks a saved
    account (sets do_switch, instant — no TUI suspend needed, it's just a
    file swap); a row's [Re-login] click (or 'R' for the current account)
    sets do_switch + do_login; '+' / a row's [+] add account also sets
    do_login alone — both start `claude auth login` in the background (the
    main loop owns the subprocess), and the popup switches to the progress
    view (login_active) until it finishes. While login_active, digit/+/r/n
    picks are ignored — only [Cancel] / esc / q / click-outside (do_cancel)
    can dismiss it, since a login is actually running.

    Returns (focus_sid, focus_bucket, panel_view, summary_tab, show_help,
    show_uerr, show_login, show_history, quit_flag, scroll_delta, do_login,
    do_retry, do_switch, do_cancel, do_prs). do_login/do_retry ask the main
    loop to start the login flow or force an immediate usage refetch — both
    bypass retry_at. do_switch is None or the slug of the saved account to
    switch to. do_cancel asks the main loop to kill an in-progress login.
    do_prs asks the main loop to switch to the PRS view (its own overlay
    state lives in run_live, handled by process_prs_input from then on)."""
    delta = 0
    quit_flag = do_prs = False
    do_login = do_retry = do_cancel = False
    do_switch = None
    for m in mouse_re.finditer(data):
        button, x, y, final = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        # Bit 6 (64) flags scroll-wheel events, which also satisfy &0b11==0;
        # exclude them from clicks so scrolling over a row doesn't open a popup.
        # Wheel up == button 64, wheel down == button 65; use them to scroll.
        if button == 64:
            delta -= 3
        elif button == 65:
            delta += 3
        elif button & 0b11 == 0 and not button & 64 and final == "M":  # left press
            if show_help:
                show_help = False          # any click dismisses help
                continue
            hit = next((tok for (tr, lo, hi, tok) in hits
                        if tr == y and lo <= x <= hi), None)
            if hit in (TAB_WIN, TAB_AW):   # switch summary window, leave overlays
                summary_tab = "aw" if hit == TAB_AW else "win"
            elif hit == "__chart__":       # drill into the clicked bucket
                focus_bucket = x - MARGIN - 1
                focus_sid, show_uerr = None, False
            elif hit == "__usage__":
                show_uerr = True
                focus_sid = focus_bucket = None   # one overlay at a time
            elif hit == "__login__":       # account string / footer "G login"
                show_login = True          # confirm before the disruptive login
                focus_sid = focus_bucket = panel_view = None
                show_uerr = False
            elif hit and hit.startswith("__acctsel__"):   # SWITCH ACCOUNT [Select]
                do_switch = hit[len("__acctsel__"):]
                show_login = False
            elif hit and hit.startswith("__acctrelogin__"):  # SWITCH ACCOUNT [Re-login]
                do_switch = hit[len("__acctrelogin__"):]
                do_login = True
            elif hit == "__acctadd__":     # SWITCH ACCOUNT [+] add account
                do_login = True
            elif hit == "__logincancel__":   # progress view [Cancel]
                do_cancel = True
                show_login = False
            elif hit in ("__history__", "__live__"):   # Live/History tab or span
                show_history = (hit == "__history__")
                focus_sid = focus_bucket = panel_view = None
                show_uerr = show_help = False
            elif hit == VIEW_PRS:          # PRs tab
                do_prs = True
                focus_sid = focus_bucket = panel_view = None
                show_uerr = show_help = False
            elif hit in ("__hsummary__", "__heatmap__"):   # footer S/M popups
                view = "hsummary" if hit == "__hsummary__" else "heatmap"
                panel_view = None if panel_view == view else view
                focus_sid = focus_bucket = None
                show_uerr = False
            elif hit == "__exit__":        # footer ⌃C span: quit
                quit_flag = True
            elif hit is not None:          # session row (incl. from a popup)
                focus_sid = hit
                focus_bucket = None
                show_uerr = False
            elif (show_login or show_uerr or focus_sid is not None
                  or focus_bucket is not None or panel_view is not None):
                if show_login and login_active:
                    do_cancel = True
                show_login = show_uerr = False
                focus_sid = focus_bucket = None
                panel_view = None
    # Strip mouse sequences, then handle keys. Arrow/PgUp/PgDn are multi-byte
    # escape SEQUENCES starting with "\x1b[" — detect and consume them FIRST so
    # a bare ESC (a "\x1b" not part of such a sequence) is the only thing that
    # triggers the close-overlay logic below.
    rest = mouse_re.sub("", data)
    for seq, step in (("\x1b[A", -1), ("\x1b[B", 1),     # arrow up / down
                      ("\x1b[5~", -10), ("\x1b[6~", 10)):  # PgUp / PgDn
        while seq in rest:
            delta += step
            rest = rest.replace(seq, "", 1)
    delta += rest.count("j") - rest.count("k")           # vim-style scroll
    if "?" in rest:
        show_help = not show_help
    # Key priority chain (one owner per pass):
    #  · switch-account modal owns the keyboard: digit picks a saved account,
    #    R re-logs in the current account, + adds a new one, anything else
    #    (N handled by the q/esc block below) cancels.
    #  · else 'g'/'G' opens that modal — the standing switch-account accelerator
    #    (also the footer "G login" and the clickable account string top-right).
    #  · else the usage-error overlay takes R (retry) / L (login), short-circuiting
    #    the menu accelerators so 'L' doesn't fall through to 'L' = Live.
    #  · else the global menu accelerators.
    if show_login and login_active:
        pass   # a login is running; only Cancel (click) / esc / q dismiss it
    elif show_login:
        saved = list_saved_accounts()
        digit = next((c for c in rest if c.isdigit() and c != "0"), None)
        if digit and int(digit) <= len(saved):
            do_switch = saved[int(digit) - 1][0]
            show_login = False
        elif "+" in rest:               # [+] add account
            do_login = True
        elif "r" in rest or "R" in rest:   # re-login current account, if saved
            do_switch = current_account_slug()
            do_login = True
        elif "n" in rest or "N" in rest:
            show_login = False
    elif "g" in rest or "G" in rest:
        show_login = True
        focus_sid = focus_bucket = panel_view = None
        show_uerr = show_help = False
    elif show_uerr:
        if "r" in rest or "R" in rest:
            do_retry = True
        if ("l" in rest or "L" in rest) and _token_stale():
            show_login = True         # confirm, then login → refetch
            show_uerr = False
    else:
        # Menu accelerators: H selects the History tab, L the Live tab (each
        # closes any open overlay/popup). Deterministic, mirroring the menu tabs.
        if "H" in rest or "h" in rest:
            show_history = True
            focus_sid = focus_bucket = panel_view = None
            show_uerr = False
        if "L" in rest or "l" in rest:
            show_history = False
            focus_sid = focus_bucket = panel_view = None
            show_uerr = False
        if "P" in rest or "p" in rest:
            do_prs = True
            focus_sid = focus_bucket = panel_view = None
            show_uerr = False
        # Panel popup toggles. In history: S = window SUMMARY, M = activity
        # heatmap (inline-fallback popups). In live: s/e/w = summary / sessions /
        # allowance. Opening a popup closes any session/bucket drill-down under it.
        keymap = ((("s", "hsummary"), ("m", "heatmap")) if show_history
                  else (("s", "summary"), ("e", "sessions"), ("w", "allow")))
        for key, view in keymap:
            if key in rest or key.upper() in rest:
                panel_view = None if panel_view == view else view
                focus_sid = focus_bucket = None
    # q or a BARE esc steps back ONE overlay level (a "\x1b[" here is an unhandled
    # CSI sequence, not a close; a lone "\x1b" is a real ESC press). With nothing
    # else open it exits the history view back to live.
    bare_esc = any(rest[i] == "\x1b" and (i + 1 >= len(rest) or rest[i + 1] != "[")
                   for i in range(len(rest)))
    if "q" in rest or bare_esc:
        if show_login:
            if login_active:
                do_cancel = True
            show_login = False
        elif show_help:
            show_help = False
        elif show_uerr:
            show_uerr = False
        elif focus_sid is not None:
            focus_sid = None
        elif focus_bucket is not None:
            focus_bucket = None
        elif panel_view is not None:
            panel_view = None
        elif show_history:
            show_history = False
    return (focus_sid, focus_bucket, panel_view, summary_tab, show_help,
            show_uerr, show_login, show_history, quit_flag, delta,
            do_login, do_retry, do_switch, do_cancel, do_prs)


def process_prs_input(data, mouse_re, hits, pr_ui, pr_rows, show_help, action_running, pr_hover):
    """Input handling for the PRS view — separate from process_input because
    its overlays (CI/comment drilldown, confirm-then-run, action progress) are
    independent of the live/history session/bucket/panel popups. Row/button
    clicks are ignored while action_running (a `gh` mutation is in flight);
    only the progress popup is shown then, with nothing to click.

    Returns (pr_ui, show_help, go_live, go_history, quit_flag, do_pr_run,
    pr_hover). go_live/go_history ask run_live to leave the PRS view.
    do_pr_run is None or (kind, row) once a confirm popup's [Y]/'y' has been
    accepted — run_live owns actually starting the `gh` subprocess
    (kick_pr_action). pr_hover is the latest (x, y) from a mode-1003
    no-button motion event (for the hover tooltip), or unchanged if none
    arrived this tick."""
    pr_ui = dict(pr_ui)
    go_live = go_history = quit_flag = False
    do_pr_run = None
    for m in mouse_re.finditer(data):
        button, x, y, final = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        if button & 32 and button & 0b11 == 3:
            pr_hover = (x, y)   # pure hover motion, no button — not a click
            continue
        if button in (64, 65) or not (button & 0b11 == 0 and not button & 64 and final == "M"):
            continue   # only plain left-press is a click here; PRS has no scroll body
        if show_help:
            show_help = False
            continue
        hit = next((tok for (tr, lo, hi, tok) in hits if tr == y and lo <= x <= hi), None)
        if action_running:
            continue    # nothing clickable while a gh mutation runs
        if hit == "__live__":
            go_live = True
        elif hit == "__history__":
            go_history = True
        elif hit == "__exit__":
            quit_flag = True
        elif hit and hit.startswith("__pr_open__"):
            if not any(v is not None for v in pr_ui.values()):   # only a bare row-click opens
                i = int(hit[len("__pr_open__"):])
                if i < len(pr_rows):
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    try:
                        subprocess.Popen([opener, pr_rows[i]["url"]],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except (OSError, subprocess.SubprocessError) as e:
                        log.warning("open %s failed: %s", pr_rows[i]["url"], e)
        elif hit and hit.startswith("__pr_ci__"):
            pr_ui["ci_idx"] = int(hit[len("__pr_ci__"):])
        elif hit and hit.startswith("__pr_comment__"):
            pr_ui["comment_idx"] = int(hit[len("__pr_comment__"):])
        elif hit and hit.startswith("__pr_confirm__"):
            kind, idx = hit[len("__pr_confirm__"):].rsplit("__", 1)
            pr_ui["confirm"] = (kind, int(idx))
        elif hit == "__pr_do_confirm__" and pr_ui["confirm"] is not None:
            kind, idx = pr_ui["confirm"]
            if idx < len(pr_rows):
                do_pr_run = (kind, pr_rows[idx])
            pr_ui["confirm"] = None
        elif hit == "__pr_do_cancel__":
            pr_ui["confirm"] = None
        elif hit is None and any(v is not None for v in pr_ui.values()):   # click outside closes
            pr_ui = {"ci_idx": None, "comment_idx": None, "confirm": None, "err": None}
    rest = mouse_re.sub("", data)
    if "?" in rest:
        show_help = not show_help
    bare_esc = any(rest[i] == "\x1b" and (i + 1 >= len(rest) or rest[i + 1] != "[")
                   for i in range(len(rest)))
    if not action_running:
        if pr_ui["confirm"] is not None:
            kind, idx = pr_ui["confirm"]
            if "y" in rest or "Y" in rest:
                if idx < len(pr_rows):
                    do_pr_run = (kind, pr_rows[idx])
                pr_ui["confirm"] = None
            elif "n" in rest or "N" in rest or "q" in rest or bare_esc:
                pr_ui["confirm"] = None
        elif "q" in rest or bare_esc:
            if show_help:
                show_help = False
            elif pr_ui["err"]:
                pr_ui["err"] = None
            elif pr_ui["ci_idx"] is not None:
                pr_ui["ci_idx"] = None
            elif pr_ui["comment_idx"] is not None:
                pr_ui["comment_idx"] = None
            else:
                go_live = True         # PRS has no parent overlay; q/esc leaves to Live
    if "H" in rest or "h" in rest:
        go_history = True
    if "L" in rest or "l" in rest:
        go_live = True
    return pr_ui, show_help, go_live, go_history, quit_flag, do_pr_run, pr_hover


if __name__ == "__main__":
    main()
