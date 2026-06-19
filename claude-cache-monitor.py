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
sessions with a prompt in the last hour and their main-vs-subagent fresh-token
balance over the last hour and the full window.

Key facts baked in:
  - 5-minute ephemeral cache == subagent/sidechain work; 1-hour cache == main
    thread (verified from the data via isSidechain).
  - Cache miss is inferred: cache_read==0 means the cached prefix was unavailable
    (e.g. expired during an idle gap) so the whole prompt was re-paid. The first
    request of a session also reads 0 - still uncached cost, shown as miss.
  - Each API response spans several JSONL lines sharing one message.id, so
    responses are de-duplicated by message.id.

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
WINDOW = timedelta(hours=12)
BUCKET = timedelta(minutes=5)
NUM_BUCKETS = int(WINDOW / BUCKET)          # 144
WINDOW_HOURS = int(WINDOW.total_seconds() // 3600)
INTERVAL_SECONDS = int(BUCKET.total_seconds())
CHART_HEIGHT = 8
MARGIN = 8                                  # left gutter for the Y-axis scale
TOTAL_WIDTH = MARGIN + NUM_BUCKETS

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
                        "claude-cache-monitor.log")
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


def short_model(m):
    """Compact model id for display: 'claude-opus-4-8' -> 'opus-4-8'."""
    if not m or m == "<synthetic>":
        return "?"
    return _clean(m[7:] if m.startswith("claude-") else m)


def new_session(sid, ts, rec):
    """Factory for a per-session stats dict. `last` means the last SUCCESSFUL
    turn ts (None until a usage record is seen); `last_act` is the last ANY
    activity ts (usage or surfaced error)."""
    return {
        "sid": sid, "last": None, "cwd": rec.get("cwd") or "",
        "main_12": 0, "sub_12": 0, "main_1h": 0, "sub_1h": 0,
        "ctx": 0, "ctx_ts": None, "model": None,
        "peak_main": 0, "peak_sub": 0, "peak_sub_model": None,
        "eff_main_1h": 0.0, "eff_main_12": 0.0,
        "eff_sub_1h": 0.0, "eff_sub_12": 0.0,
        "subs": {},   # agentId -> per-subagent detail
        "buckets": [empty_bucket() for _ in range(NUM_BUCKETS)],
        "err": None, "last_act": ts,
    }


def collect(now: datetime):
    """Return (buckets, sessions): time buckets oldest->newest plus per-session
    cache stats, all from de-duplicated usage records."""
    cutoff = now - WINDOW
    last_hour = now - timedelta(hours=1)
    mtime_floor = cutoff.timestamp() - 1
    buckets = [empty_bucket() for _ in range(NUM_BUCKETS)]
    sessions: dict[str, dict] = {}
    seen: set[str] = set()

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
                    msg = rec.get("message") or {}
                    ts = parse_ts(rec.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    sid = rec.get("sessionId") or os.path.basename(path)[:-6]

                    # Surfaced API failures (synthetic assistant records) carry
                    # no usage, so they'd be skipped by the usage check below.
                    # Handle them first: record the latest error per session.
                    if msg.get("isApiErrorMessage"):
                        s = sessions.get(sid) or sessions.setdefault(sid, new_session(sid, ts, rec))
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

                    idx = int((ts - cutoff) / BUCKET)
                    idx = min(max(idx, 0), NUM_BUCKETS - 1)
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
                    eff = inp + 1.25 * f5 + 2 * f1 + 0.1 * read
                    model = msg.get("model")

                    # Charts 1 & 2 + output/responses for the global bucket.
                    add_usage(b, inp, f5, f1, read, fresh, out)

                    # Per-session drill-down: split fresh tokens (new work) by
                    # main thread vs subagent (sidechain). Fresh, not total
                    # input, so the main thread's huge cheap cache reads don't
                    # drown the subagent signal.
                    side = "sub" if rec.get("isSidechain") else "main"
                    s = sessions.get(sid)
                    if s is None:
                        s = sessions[sid] = new_session(sid, ts, rec)
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
                            sub = s["subs"][aid] = {
                                "slug": _clean(rec.get("slug") or aid[:12]),
                                "start": ts, "stop": ts, "peak": 0, "eff": 0.0,
                                "model": model}
                        sub["start"] = min(sub["start"], ts)
                        sub["stop"] = max(sub["stop"], ts)
                        sub["peak"] = max(sub["peak"], total_in)
                        sub["eff"] += eff
                        if rec.get("slug"):
                            sub["slug"] = _clean(rec["slug"])
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


def render_chart(title, keys, buckets, height, now, anim=0):
    totals = [sum(b[k] for k in keys) for b in buckets]
    maxt = max(totals) if totals else 0
    columns = [build_column([(CO[k], b[k]) for k in keys], tot, maxt, height)
               for b, tot in zip(buckets, totals)]

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

    # X-axis baseline + absolute local hour ticks (H:00 only).
    axis = [" "] * NUM_BUCKETS
    local_cut = (now - WINDOW).astimezone()
    local_now = now.astimezone()
    tick = local_cut.replace(minute=0, second=0, microsecond=0)
    if tick < local_cut:
        tick += timedelta(hours=1)
    while tick <= local_now:
        pos = round((tick - local_cut).total_seconds() / BUCKET.total_seconds())
        lab = f"{tick.hour}:00"
        start = min(pos, NUM_BUCKETS - len(lab))
        for i, ch in enumerate(lab):
            if 0 <= start + i < NUM_BUCKETS:
                axis[start + i] = ch
        tick += timedelta(hours=1)
    lines.append(rgb(DIM, "0".rjust(MARGIN - 1)) + " "
                 + rgb(DIM2, "└" + "─" * (NUM_BUCKETS - 1)))
    lines.append(" " * MARGIN + rgb(DIM, "".join(axis)))
    return lines


def legend(items):
    return "   ".join(rgb(CO[k], CHIP) + " " + rgb(DIM, label) for k, label in items)


# ── panels ───────────────────────────────────────────────────────────────────

def panel(title, rows, inner):
    fill = max(inner - 3 - _visible_len(title), 0)
    out = [rgb(DIM2, "╭─ ") + rgb(ACCENT, title, bold=True)
           + rgb(DIM2, " " + "─" * fill + "╮")]
    for r in rows:
        out.append(rgb(DIM2, "│") + _padcol(r, inner) + rgb(DIM2, "│"))
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


def summary_rows(buckets, inner):
    agg = empty_bucket()
    for b in buckets:
        for k in agg:
            agg[k] += b[k]
    total_input = agg["read"] + agg["new"] + agg["miss"]

    def kv(label, value):
        return label + " " * max(inner - _visible_len(label) - _visible_len(value), 1) + value

    def meter(key, name, value):
        return kv(rgb(CO[key], CHIP) + " " + rgb(TEXT, name), rgb(TEXT, value))

    def beff(bs):
        return sum(b["uncached"] + 1.25 * b["c5m"] + 2 * b["c1h"] + 0.1 * b["read"]
                   for b in bs)

    eff_12 = beff(buckets)
    eff_1h = beff(buckets[-12:])     # 12 × 5-min buckets ≈ last hour

    return [
        kv(rgb(DIM, "input tokens"), rgb(TEXT, fmt(total_input), bold=True)),
        kv(rgb(DIM, "output tokens"), rgb(TEXT, fmt(agg["output"]), bold=True)),
        kv(rgb(DIM, f"responses · {WINDOW_HOURS}h"), rgb(TEXT, fmt(agg["responses"]))),
        kv(rgb(DIM, "effective tokens · 1h/12h"),
           rgb(TEXT, fmt_compact(round(eff_1h)) + " / " + fmt_compact(round(eff_12)),
               bold=True)),
        rgb(DIM2, "─" * inner),
        meter("c5m", "5m cache · subagent", pct(agg["c5m"], total_input)),
        meter("c1h", "1h cache · main", pct(agg["c1h"], total_input)),
        meter("read", "read from cache", pct(agg["read"], total_input)),
        meter("miss", "cache miss", pct(agg["miss"], total_input)),
    ]


def session_rows(sessions, now, inner):
    """Return (rows, active_sids). active_sids is the ordered list of session
    ids for the DATA rows (header excluded), in the same order as the rows, so
    callers can map a clicked row index back to its session."""
    last_hour = now - timedelta(hours=1)
    active = sorted((s for s in sessions.values() if s["last_act"] >= last_hour),
                    key=lambda s: s["last_act"], reverse=True)
    rows = [rgb(DIM, f"  {'last':<6}{'project':<16}{'session':<8}"
                     f"{'1h main/sub':<20}{'12h main/sub':<20}{'context':<12}")]
    if not active:
        rows.append(rgb(DIM, "  no sessions active in the last hour"))
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
        proj = _clean(os.path.basename(s["cwd"]) or "?")[:15]
        # Errored rows: red ! marker (2 cols, same as the plain indent), red
        # project + time. Everything else keeps its normal colour.
        indent = rgb(HOT_C, "! ") if errored_last else "  "
        when_col = (rgb(HOT_C, f"{when:<6}") if errored_last
                    else rgb(ACCENT, f"{when:<6}"))
        proj_col = (rgb(HOT_C, f"{proj:<16}") if errored_last
                    else rgb(TEXT, f"{proj:<16}"))
        rows.append(
            indent + when_col + proj_col
            + rgb(DIM, f"{s['sid'][:8]:<8}")
            + _padcol(bal(s["main_1h"], s["sub_1h"]), 20)
            + _padcol(bal(s["main_12"], s["sub_12"]), 20)
            + _padcol(ctx_cell(s), 12)
        )
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
    head = (rgb(TEXT, proj, bold=True) + rgb(DIM, "  " + s["sid"][:8] + "  ")
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
    body += ["  " + legend([("uncached", "uncached"),
                            ("c5m", "5m · subagent"), ("c1h", "1h · main")])]
    body += render_chart("Input tokens · cache write disposition",
                         ["uncached", "c5m", "c1h"], sb, 5, now, anim)
    body += ["", "  " + legend([("read", "from cache"),
                                ("new", "new input"), ("miss", "cache miss")])]
    body += render_chart("Context assembly", ["read", "new", "miss"], sb, 5, now, anim)
    body += ["", "  " + legend([("output", "output tokens")])]
    body += render_chart("Output tokens generated", ["output"], sb, 5, now, anim)

    # Named subagent detail table — subagents active in the LAST HOUR only.
    last_hour = now - timedelta(hours=1)
    cands = sorted((sub for sub in s["subs"].values() if sub["stop"] >= last_hour),
                   key=lambda sub: sub["start"])
    body += ["", "  " + rgb(DIM, "subagents · last 1h")]
    if not cands:
        body += ["  " + rgb(DIM, "no subagents in the last hour")]
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
        # Cap the table to whatever vertical space remains. The popup is the
        # body plus 2 border rows; it must not exceed the terminal height.
        # Reserve room for the footer (blank + close hint = 2) we add below.
        footer_lines = 2
        used = len(body) + 2 + footer_lines + 1   # +1 for the header row
        room = max(rows - used, 0)
        detail_rows = []
        if room <= 0:
            # No room even for one row: show how many were dropped.
            detail_rows = ["  " + rgb(DIM, f"+{len(cands)} more (by eff)")]
        elif len(cands) <= room:
            detail_rows = [header] + [sub_row(sub) for sub in cands]
        else:
            # Keep the largest-by-eff that fit; reserve one row for the "+k more".
            keep = sorted(cands, key=lambda sub: sub["eff"], reverse=True)[:room - 1]
            keep_ids = {id(sub) for sub in keep}
            shown = [sub for sub in cands if id(sub) in keep_ids]  # timeline order
            detail_rows = ([header] + [sub_row(sub) for sub in shown]
                           + ["  " + rgb(DIM, f"+{len(cands) - len(shown)} more (by eff)")])
        body += detail_rows

    body += ["", "  " + rgb(DIM, "click outside · q · esc to close")]
    return panel("SESSION DETAIL", body, inner)


def render_help(now, cols, rows, scroll):
    """A modal explaining every element of the dashboard and how to read it.

    Sized to ~75% of the terminal and word-wrapped so nothing overflows
    horizontally; scrollable, with a vertical scrollbar in the last column when
    the content is taller than the visible area.

    Content is authored as typed items so colour survives wrapping:
      ("H",  text)      heading  (cyan, bold; never wraps — keep short)
      ("L",  text)      legend   (pre-coloured single line; never wraps)
      ("T",  text)      prose    (plain str, single colour; wrapped + DIM'd)
      ("G",  None)      gap      (one blank line)

    Returns (panel_lines, max_scroll)."""
    inner = max(int(cols * 0.75) - 2, 40)      # content width inside borders
    ph = max(int(rows * 0.75), 12)             # total panel height
    twidth = inner - 2                         # wrap prose narrower, leaving the
    #                                            last 2 cols for gap + scrollbar.

    def ch(k):                                  # colour chip for a palette key
        return rgb(CO[k], CHIP)

    items = [
        ("T", f"Live Claude Code cache-token usage, last {WINDOW_HOURS}h in "
              f"5-minute buckets. Charts refresh every {INTERVAL_SECONDS // 60}m; "
              "the screen animates."),
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
              "1.25x 5m-write + 2x 1h-write + 0.1x cache-read."),
        ("G", None),
        ("H", "SUMMARY"),
        ("T", "12h totals: input, output, responses, effective tokens (1h / 12h), "
              "plus the cache-mix chips."),
        ("G", None),
        ("H", "ACTIVE SESSIONS"),
        ("T", "Sessions active in the last hour. \"1h / 12h\" = fresh-token split, "
              "main vs subagent. A session turns RED with a ! when its most recent "
              "action hit a surfaced API error."),
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
        ("T", "Opens its detail: that session's 3 charts, effective tokens "
              "(main/sub, 1h & 12h), named subagents from the last hour (peak ctx "
              "+ eff tkn), and any recent error."),
        ("G", None),
        ("H", "KEYS"),
        ("T", "? help · up/down PgUp/PgDn j/k scroll · q / esc close · ^C quit."),
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

    vis = ph - 2                                # panel() uses 2 rows for borders
    max_scroll = max(0, len(lines) - vis)
    scroll = max(0, min(scroll, max_scroll))

    if len(lines) <= vis:
        body = lines                            # fits; let panel pad. No scrollbar.
    else:
        window = lines[scroll:scroll + vis]
        # Scrollbar track of `vis` cells: thumb height proportional to the
        # visible fraction, thumb position proportional to the scroll fraction.
        thumb = max(1, round(vis * vis / len(lines)))
        top = round(scroll / max_scroll * (vis - thumb)) if max_scroll else 0
        top = max(0, min(top, vis - thumb))
        body = []
        for i in range(vis):
            cell = (rgb(ACCENT, "█") if top <= i < top + thumb
                    else rgb(DIM2, "│"))
            body.append(_padcol(window[i], inner - 1) + cell)

    return panel("HELP · how to read this dashboard", body, inner), max_scroll


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


def allowance_rows(now, anim, inner):
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
    for kind, label in (("session", "5-hour session"), ("weekly_all", "weekly")):
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


def render_frame(now, buckets, sessions, anim=0, height=CHART_HEIGHT):
    """Return (frame_str, hits). hits maps clickable session rows to ids:
    [(term_row, x_lo, x_hi, sid), ...], one per ACTIVE-SESSIONS data row,
    using 1-based terminal coordinates."""
    local = now.astimezone()
    title = "CLAUDE CODE · CACHE TELEMETRY"
    clock = f"{local:%a %d %b · %H:%M:%S %Z}"
    pad = max(TOTAL_WIDTH - _visible_len(title) - len(clock) - 4, 1)

    out = [
        " " + rgb(ACCENT, "◆ ", bold=True) + rgb(TEXT, title, bold=True)
        + " " * pad + rgb(DIM, clock),
        grad_rule(TOTAL_WIDTH, ACCENT2, ACCENT),
        "",
        "  " + legend([("uncached", "uncached"),
                       ("c5m", "5m · subagent"), ("c1h", "1h · main")]),
    ]
    out += render_chart("Input tokens · cache write disposition",
                        ["uncached", "c5m", "c1h"], buckets, height, now, anim)
    out += ["",
            "  " + legend([("read", "from cache"),
                           ("new", "new input"), ("miss", "cache miss")])]
    out += render_chart("Context assembly",
                        ["read", "new", "miss"], buckets, height, now, anim)
    out += ["", "  " + legend([("output", "output tokens")])]
    out += render_chart("Output tokens generated",
                        ["output"], buckets, height, now, anim)

    out += [""]
    summ_inner, sess_inner, allow_inner = 40, 84, 26
    sess_rows, active_sids = session_rows(sessions, now, sess_inner)
    summ = panel("SUMMARY · 12h", summary_rows(buckets, summ_inner), summ_inner)
    sess = panel("ACTIVE SESSIONS · last 1h", sess_rows, sess_inner)
    # Loading dots tick ~1 Hz (not the 10 Hz chart shimmer) so they don't blur.
    slow = int(now.timestamp())
    allow = panel("ALLOWANCE", allowance_rows(now, slow, allow_inner), allow_inner)

    # Hit map: the sessions panel sits in the middle column. Within its column,
    # line 0 is the panel title border, line 1 is the column header, lines 2..
    # are data rows. panel_start is the 0-based `out` index of the hjoin block's
    # first line, so data row j is at out index panel_start+2+j -> terminal row
    # panel_start+2+j+1 (1-based).
    gap = 3
    summ_total = summ_inner + 2          # inner + 2 border columns
    sess_total = sess_inner + 2
    x_lo = summ_total + gap + 1
    x_hi = summ_total + gap + sess_total
    panel_start = len(out)
    out += hjoin(summ, sess, allow, gap=gap)
    # 4th tuple element is a TOKEN: a session sid, or the sentinel "__usage__".
    hits = [(panel_start + 2 + j + 1, x_lo, x_hi, sid)
            for j, sid in enumerate(active_sids)]
    # Make the ALLOWANCE panel clickable when there's a live usage error to show
    # (opens the USAGE CALL ERROR overlay). Its column span sits to the right of
    # the summary + sessions panels.
    if _usage.get("err") and _usage["err"] != "loading…":
        allow_lo = summ_total + gap + sess_total + gap + 1
        allow_hi = allow_lo + (allow_inner + 2) - 1
        hits += [(panel_start + k + 1, allow_lo, allow_hi, "__usage__")
                 for k in range(len(allow))]

    plan = " · ".join(p for p in (_usage.get("sub"), _usage.get("tier")) if p)
    stamp = _usage["at"].astimezone().strftime("%H:%M:%S") if _usage.get("at") else "—"
    out += ["", "  " + rgb(DIM, f"plan {plan or '?'}   ·   allowance live, "
                           f"updated {stamp}   ·   charts every "
                           f"{INTERVAL_SECONDS // 60}m   ·   ? help   ·   ⌃C to exit")]
    return "\n".join(out), hits


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--interval", type=int, default=INTERVAL_SECONDS,
                    metavar="SECONDS", help="seconds between refreshes (default 300)")
    args = ap.parse_args()

    if args.once:
        now = datetime.now(timezone.utc)
        fetch_usage()                       # synchronous: single frame needs it
        buckets, sessions = collect(now)
        height = CHART_HEIGHT
        if sys.stdout.isatty():
            try:
                _, rows = os.get_terminal_size()
                height = fit_height(rows, sessions, now)
            except OSError:
                pass
        frame, _hits = render_frame(now, buckets, sessions, height=height)
        print(frame)
        return

    run_live(args)


def fit_height(rows, sessions, now):
    """Pick a chart height so the whole frame fits in `rows` terminal lines with
    ~2 spare, killing the overflow that caused scroll/flicker on short screens.

    Fixed (non-bar) chrome = everything except the 3*height bar rows:
      banner+rule+blank+legend (4) + 3 legends-between/blanks (handled below)
      + per-chart title+baseline+axis (3 each -> 9) + blank before panels (1)
      + panels block (2 borders + body) + blank + footer (2).
    """
    last_hour = now - timedelta(hours=1)
    n_active = sum(1 for s in sessions.values() if s["last_act"] >= last_hour)
    panels_body = max(8, 1 + n_active, len(allowance_rows(now, 0, 26)))
    # 4 (header block) + 3*2 (blank+legend before charts 2&3 plus first legend
    # already counted) ... count explicitly to avoid drift:
    fixed_chrome = (
        4                       # banner, rule, blank, legend-1
        + 3                     # chart-1 title+baseline+axis
        + 2 + 3                 # blank+legend-2, chart-2 chrome
        + 2 + 3                 # blank+legend-3, chart-3 chrome
        + 1                     # blank before panels
        + 2 + panels_body       # panels block (2 borders + body)
        + 2                     # blank + footer
    )
    height = (rows - fixed_chrome - 2) // 3
    return max(3, min(10, height))


def run_live(args):
    log.info("dashboard start: interval=%ss tick=%ss", args.interval, TICK_SECONDS)
    alt = sys.stdout.isatty()
    fd = sys.stdin.fileno() if alt else None
    old_term = None
    if alt:
        sys.stdout.write("\033[?1049h\033[?25l")   # alt screen, hide cursor
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
    anim = 0
    hits = []
    focus_sid = None
    show_help = False
    show_uerr = False
    help_scroll = 0
    prev_show_help = False
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

            height = fit_height(rows, sessions, now) if alt else CHART_HEIGHT
            # Fast repaint every tick: animates loading, keeps the clock live,
            # and surfaces the background usage fetch within ~1s of completion.
            frame, hits = render_frame(now, buckets, sessions, anim, height)
            # Too small to fit? The frame would overflow and scroll, desyncing the
            # click hit-regions onto the wrong rows. Show a notice and drop hits so
            # clicks can't misfire; close any overlay until there's room again.
            if alt and (cols < TOTAL_WIDTH or frame.count("\n") + 1 > rows):
                hits = []
                show_help = show_uerr = False
                focus_sid = None
                frame = render_too_small(cols, rows, height * 3 + 30)
            if alt:
                # One overlay at a time: help > usage-error > popup.
                overlay, okey = None, None
                if show_help:
                    if not prev_show_help:        # freshly opened: reset scroll
                        help_scroll = 0
                    overlay, max_scroll = render_help(now, cols, rows, help_scroll)
                    help_scroll = max(0, min(help_scroll, max_scroll))
                    okey = "help"
                elif show_uerr:
                    overlay = render_usage_error(now, cols, rows)
                    if overlay is None:
                        show_uerr = False
                    else:
                        okey = "uerr"
                elif focus_sid is not None:
                    overlay = render_popup(focus_sid, sessions, now, cols, rows, anim)
                    if overlay is None:
                        focus_sid = None
                    else:
                        okey = ("popup", focus_sid)
                if overlay is None:
                    # No overlay: full base repaint each tick (shimmer live).
                    body = "\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J"
                    sys.stdout.write(body)
                else:
                    oh = len(overlay)
                    ow = max((_visible_len(x) for x in overlay), default=0)
                    row0 = max((rows - oh) // 2, 1)
                    col0 = max((cols - ow) // 2, 1)
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
                prev_show_help = show_help
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
                    focus_sid, show_help, show_uerr, scroll_delta = process_input(
                        data, mouse_re, hits, focus_sid, show_help, show_uerr)
                    # Only the help overlay scrolls; the delta is clamped each
                    # render and ignored when help is closed (reset on open).
                    help_scroll += scroll_delta
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
            sys.stdout.write("\033[?25h\033[?1049l")   # show cursor, leave alt
            sys.stdout.flush()


def process_input(data, mouse_re, hits, focus_sid, show_help, show_uerr):
    """Update (focus_sid, show_help, show_uerr) from a chunk of terminal input
    and return a scroll delta for the (only scrollable) help overlay.

    A left-click on a session row opens/switches its popup; a click on the
    ALLOWANCE panel (token "__usage__") opens the usage-error overlay; a click
    outside closes whatever overlay is open. '?' toggles help; q/bare-esc closes
    every overlay. Mouse wheel and arrow/PgUp/PgDn/j/k scroll the help overlay.

    Returns (focus_sid, show_help, show_uerr, scroll_delta)."""
    delta = 0
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
            if hit == "__usage__":
                show_uerr = True
                focus_sid = None           # one overlay at a time
            elif hit is not None:
                focus_sid = hit
                show_uerr = False
            elif show_uerr or focus_sid is not None:
                show_uerr = False
                focus_sid = None
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
    # Close on q or a BARE esc only. Any remaining "\x1b[" here is a CSI sequence
    # we don't handle (not a close); a lone "\x1b" with nothing after it is a
    # real ESC press → close.
    bare_esc = any(rest[i] == "\x1b" and (i + 1 >= len(rest) or rest[i + 1] != "[")
                   for i in range(len(rest)))
    if "q" in rest or bare_esc:
        show_help = False
        focus_sid = None
        show_uerr = False
    return focus_sid, show_help, show_uerr, delta


if __name__ == "__main__":
    main()
