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

Press H (or click "H history" in the footer) for a longer-span HISTORY view:
the same three charts over a configurable window (--history-hours, default 168 =
1 week) with an auto-scaled bucket and a day-by-day axis, a SUMMARY with a $ cost
estimate and cache-hit rate, and click-a-bar drill-down. No active-sessions or
allowance panels there.

Stdlib only. --once prints a single frame; --interval overrides the period.
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
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TRANSCRIPT_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
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
USAGE_REFRESH = 300                 # seconds between live-usage refetches
USAGE_BACKOFF = 900                 # after a 429, wait this long before retrying

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "claude-dashboard.log")
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ccmon")


def rgb(c, text, bold=False):
    r, g, b = c
    b0 = "\033[1m" if bold else ""
    return f"{b0}\033[38;2;{r};{g};{b}m{text}\033[0m"


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
    # API after the fact. So we grade against the model's *capability*: Opus and
    # Sonnet 4.x support the 1M beta -> grade at 1M (a real 1M session then never
    # false-flashes at 175k); Haiku / older / unknown cap at 200k. Trade-off: an
    # Opus/Sonnet run in plain 200k mode under-warns (won't alarm near its 200k
    # wall) — acceptable, since the 1M beta is opt-in and the alarm is for big
    # contexts.
    if not model:
        return 200_000
    m = model.lower()
    if ("opus" in m or "sonnet" in m) and "-4" in m:
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


def collect(now: datetime, window=None, bucket=None, num_buckets=None,
            track_models=False):
    """Return (buckets, sessions): time buckets oldest->newest plus per-session
    cache stats, all from de-duplicated usage records. `window`/`bucket`/
    `num_buckets` default to the live globals; the history view passes its own
    (longer) span and coarser bucket so the same scan feeds both views.
    `track_models` adds a per-bucket {model: effective-tokens} map under the
    extra "models" key (ignored by the fixed-key aggregation loops) for the
    history model-mix chart."""
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
    sessions: dict[str, dict] = {}
    seen: set[str] = set()
    titles: dict[str, str] = {}   # sid -> custom session title (latest /rename)

    for path in glob.glob(TRANSCRIPT_GLOB, recursive=True):
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

                    # Surfaced API failures (synthetic assistant records) carry
                    # no usage, so they'd be skipped by the usage check below.
                    # Handle them first: record the latest error per session.
                    if msg.get("isApiErrorMessage"):
                        s = sessions.get(sid) or sessions.setdefault(sid, new_session(sid, ts, rec, num_buckets))
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

                    # Per-session drill-down: split fresh tokens (new work) by
                    # main thread vs subagent (sidechain). Fresh, not total
                    # input, so the main thread's huge cheap cache reads don't
                    # drown the subagent signal.
                    side = "sub" if rec.get("isSidechain") else "main"
                    s = sessions.get(sid)
                    if s is None:
                        s = sessions[sid] = new_session(sid, ts, rec, num_buckets)
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

    # Attach custom /rename titles to their sessions (titles may be seen before
    # the session has any usage record, so this is done after the full scan).
    for tsid, t in titles.items():
        s = sessions.get(tsid)
        if s is not None:
            s["name"] = t

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
    """One of the three side panels rendered as a modal (used on terminals too
    narrow/short to show them inline). Returns (lines, regions) where regions =
    [(overlay_line, lo_off, hi_off, token)] are clickable spans in coordinates
    relative to the overlay box; the caller offsets them by the box origin."""
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
        ("T", f"Press H (or click \"H history\" in the footer) for a longer-span "
              f"view — default last {fmt_window(HIST_WINDOW)}, configurable via "
              "--history-hours. Same charts with a coarser auto-scaled bucket and "
              "a day-by-day axis, plus a SUMMARY with a $ cost estimate and cache-"
              "hit rate. Click a bar to break the slice down by session. H or q "
              "returns to live."),
        ("G", None),
        ("H", "KEYS"),
        ("T", "? help · H history · s/e/w panels · click bar/session/tab · "
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
    footer = ["", rgb(DIM, "click outside · q · esc to close")]
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


# ── live bundled-allowance usage (GET /api/oauth/usage, same as `/usage`) ─────

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
# Shared by the context light (ctx_grade) and allowance gauge (gauge_grade) —
# the actual thresholds live in those functions, not here.
OK_C = (52, 224, 150)       # green
WARN_C = (255, 205, 82)     # yellow
HOT_C = (255, 88, 96)       # red
ORANGE_C = (255, 138, 56)   # amber

# Shared with the render thread; the network call must never block a frame.
_usage = {"data": None, "err": "loading…", "at": None, "sub": None, "tier": None,
          "retry_at": None, "err_body": None}
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
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {tok}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-cli/cache-monitor",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            data = json.load(r)
        _usage_set(data=data, err=None, retry_at=None, at=datetime.now(timezone.utc),
                   sub=oa.get("subscriptionType"), tier=oa.get("rateLimitTier"),
                   err_body=None)
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
    "__history__" (toggle history view), or "__exit__" (quit). `layout` is a
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
        title = ("CLAUDE CODE · HISTORY · last " + fmt_window(HIST_WINDOW)
                 if mode == "history" else "CLAUDE CODE · CACHE TELEMETRY")
        clock = f"{local:%a %d %b · %H:%M:%S %Z}"
        pad = max(TOTAL_WIDTH - _visible_len(title) - len(clock) - 4, 1)
        out += [
            " " + rgb(ACCENT, "◆ ", bold=True) + rgb(TEXT, title, bold=True)
            + " " * pad + rgb(DIM, clock),
            grad_rule(TOTAL_WIDTH, ACCENT2, ACCENT),
            "",
        ]

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
        # History: one SUMMARY panel (input/output/responses/effective + $ cost +
        # cache-hit + cache-mix), scoped to the week. No sessions/allowance.
        if layout.get("history_summary"):
            inner = layout["panel_cfg"]["summ_inner"]
            srows = summary_rows(buckets, inner, fmt_window(HIST_WINDOW),
                                 lean=layout["panels_lean"], show_cost=True)
            out.append("")
            out += panel("SUMMARY · last " + fmt_window(HIST_WINDOW), srows, inner)
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
            foot = (f"history · {fmt_window(HIST_BUCKET)} buckets   ·   "
                    f"click a bar   ·   H live   ·   ? help   ·   ⌃C to exit")
            hist_tok = "H live"
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
                    f"H history   ·   ? help   ·   ⌃C to exit")
            hist_tok = "H history"
        foot = foot[:TOTAL_WIDTH - 2]          # clip so it never wraps/overflows
        out += ["", "  " + rgb(DIM, foot)]
        # Clickable footer spans: the H phrase toggles the view, "⌃C to exit"
        # quits. Coords are 1-based; the line has a 2-col indent, so a substring
        # at plain index i sits at terminal column i+3. Skip any clipped off.
        foot_row = len(out)
        for sub, tok in ((hist_tok, "__history__"), ("⌃C to exit", "__exit__")):
            i = foot.find(sub)
            if i >= 0:
                lo = i + 3
                hits.append((foot_row, lo, lo + _visible_len(sub) - 1, tok))
    return "\n".join(out), hits


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
    if aw_hours >= win_min / 60:
        fail(f"--active-window-hours ({aw_hours:g}) must be < the window "
             f"({win_min / 60:g}h)")

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


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    args = ap.parse_args()

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
        # 4 charts (3 standard + model-mix) and one SUMMARY panel (with $ cost +
        # cache-hit); no sessions/allowance. Recompute the chart base for 4.
        ncharts = 4
        hbase = ncharts * per_chart + ((ncharts - 1) if L["chart_blanks"] else 0)
        if L["page_title"]:
            hbase += 3
        if L["footer"]:
            hbase += 2
        summ = (6 if lean else 11)          # summary_rows length incl. cost lines
        panel_body = 1 + 2 + summ           # blank + borders + body
        if (rows - hbase - panel_body) // ncharts < MIN_BAR_H:
            panel_body = 0                  # no room — drop the panel, charts grow
        L["panels_inline"] = panel_body > 0
        L["history_summary"] = panel_body > 0
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
            sys.stdout.write("\033[?1000h\033[?1006h")
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
    anim = 0
    hits = []
    focus_sid = None
    focus_bucket = None
    panel_view = None            # None | "summary" | "sessions" | "allow"
    summary_tab = "win"
    show_help = False
    show_uerr = False
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
            # Heavy transcript scan only every --interval; allowance more often.
            if last_collect is None or (now - last_collect).total_seconds() >= args.interval:
                buckets, sessions = collect(now)
                last_collect = now
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
            if show_history:
                if (last_hist_collect is None
                        or (now - last_hist_collect).total_seconds() >= args.interval):
                    hist_buckets, hist_sessions = collect(
                        now, HIST_WINDOW, HIST_BUCKET, HIST_NUM_BUCKETS,
                        track_models=True)
                    last_hist_collect = now
                cur_buckets, cur_sessions, cur_mode = hist_buckets, hist_sessions, "history"
            else:
                cur_buckets, cur_sessions, cur_mode = buckets, sessions, "live"
            layout = (plan_layout(rows, cols, cur_sessions, now,
                                  history=show_history) if alt else None)
            # Fast repaint every tick: animates loading, keeps the clock live,
            # and surfaces the background usage fetch within ~1s of completion.
            frame, hits = render_frame(now, cur_buckets, cur_sessions, anim, layout,
                                       summary_tab, cols=cols, rows=rows,
                                       mode=cur_mode)
            # Too small to fit? The frame would overflow and scroll, desyncing the
            # click hit-regions onto the wrong rows. Show a notice and drop hits so
            # clicks can't misfire; close any overlay until there's room again.
            if alt and (cols < TOTAL_WIDTH or rows < 9
                        or frame.count("\n") + 1 > rows):
                hits = []
                show_help = show_uerr = False
                panel_view = None
                focus_sid = focus_bucket = None
                frame = render_too_small(cols, rows, 9)
            if alt:
                # One overlay at a time: help > usage-error > session > bucket >
                # panel popup. overlay_regions carries a modal popup's clickable
                # spans (relative to the box); translated to screen hits below.
                overlay, okey, overlay_regions = None, None, []
                if show_help:
                    overlay = render_help(now, cols, rows)
                    okey = "help"
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
                    # only redraw the overlay box in place each tick. Repainting
                    # the whole base every tick under the overlay — with its full-
                    # screen clear — is what made it flicker (the region flashed
                    # base→overlay every frame). Freezing the base kills the
                    # flicker; the base shimmer just pauses while an overlay is up.
                    # Overlay lines are padded to a constant width so each redraw
                    # fully overwrites the previous one without an intervening clear.
                    if okey != prev_okey:
                        body = "\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J"
                        sys.stdout.write(body)
                    for k, pl in enumerate(overlay):
                        sys.stdout.write(f"\033[{row0 + k};{col0}H" + _padcol(pl, ow))
                prev_okey = okey
            else:
                sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            anim += 1

            # Input-aware wait: wake early on a click/keypress for ≤1-tick latency.
            if alt and old_term is not None:
                r, _, _ = select.select([sys.stdin], [], [], TICK_SECONDS)
                if r:
                    try:
                        data = os.read(fd, 4096).decode("utf-8", "ignore")
                    except OSError:
                        data = ""
                    (focus_sid, focus_bucket, panel_view, summary_tab, show_help,
                     show_uerr, show_history, quit_flag, scroll_delta) = process_input(
                        data, mouse_re, hits, focus_sid, focus_bucket,
                        panel_view, summary_tab, show_help, show_uerr, show_history)
                    if quit_flag:              # footer "⌃C to exit" was clicked
                        break
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
                    sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
                except (termios.error, ValueError, OSError):
                    pass
            sys.stdout.write("\033[?7h\033[?25h\033[?1049l")   # re-enable wrap, show cursor, leave alt
            sys.stdout.flush()


def process_input(data, mouse_re, hits, focus_sid, focus_bucket, panel_view,
                  summary_tab, show_help, show_uerr, show_history):
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

    'H' (or a click on the footer "H history"/"H live" span) toggles the history
    view; a click on the footer "⌃C to exit" span requests quit.

    Returns (focus_sid, focus_bucket, panel_view, summary_tab, show_help,
    show_uerr, show_history, quit_flag, scroll_delta)."""
    delta = 0
    quit_flag = False
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
            elif hit == "__history__":     # footer H span: toggle history view
                show_history = not show_history
                focus_sid = focus_bucket = panel_view = None
                show_uerr = show_help = False
            elif hit == "__exit__":        # footer ⌃C span: quit
                quit_flag = True
            elif hit is not None:          # session row (incl. from a popup)
                focus_sid = hit
                focus_bucket = None
                show_uerr = False
            elif (show_uerr or focus_sid is not None or focus_bucket is not None
                  or panel_view is not None):
                show_uerr = False
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
    # H (either case) toggles the history view; closes any open overlay/popup.
    if "H" in rest or "h" in rest:
        show_history = not show_history
        focus_sid = focus_bucket = panel_view = None
        show_uerr = False
    # s/e/w toggle the panel popups (summary / sessions [e]ntries / [w]allowance).
    # Opening a panel closes any session/bucket drill-down underneath it. Disabled
    # in history view (those panels are live-only).
    if not show_history:
        for key, view in (("s", "summary"), ("e", "sessions"), ("w", "allow")):
            if key in rest:
                panel_view = None if panel_view == view else view
                focus_sid = focus_bucket = None
    # q or a BARE esc steps back ONE overlay level (a "\x1b[" here is an unhandled
    # CSI sequence, not a close; a lone "\x1b" is a real ESC press). With nothing
    # else open it exits the history view back to live.
    bare_esc = any(rest[i] == "\x1b" and (i + 1 >= len(rest) or rest[i + 1] != "[")
                   for i in range(len(rest)))
    if "q" in rest or bare_esc:
        if show_help:
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
            show_uerr, show_history, quit_flag, delta)


if __name__ == "__main__":
    main()
