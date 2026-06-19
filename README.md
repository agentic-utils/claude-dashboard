# claude-dashboard

A live terminal dashboard for **Claude Code cache-token usage**. Pure Python
stdlib, single file, 24-bit truecolour. It scans your Claude Code transcripts
under `~/.claude/projects/**/*.jsonl`, reads the per-response `usage` data, and
paints an auto-updating view of the last 12 hours.

![charts: input cache-write disposition, context assembly, output](#)

## What it shows

**Three stacked bar charts** (12h window, 5-minute buckets, shimmering glow,
Y-axis token scale, hourly X-axis):

1. **Input · cache-write disposition** — how fresh input was cached: uncached /
   written to 5-minute cache (subagent work) / written to 1-hour cache (main thread).
2. **Context assembly** — how each prompt was built: read from cache / new input /
   cache miss (a turn that read 0 from cache — prefix expired in an idle gap, or the
   session's first turn).
3. **Output** — tokens generated.

**SUMMARY** — 12h totals: input, output, responses, **effective tokens** (1h / 12h)
and the cache mix. Effective tokens = true cost in token-equivalents:
`1× uncached + 1.25× 5m-write + 2× 1h-write + 0.1× cache-read`.

**ACTIVE SESSIONS** — every session with a prompt in the last hour: last activity,
project, main-vs-subagent fresh-token split (1h / 12h), and a **context-size
traffic light** (green → yellow → amber → red → flashing red, scaled to the model's
200k or 1M window). A session turns red with a `!` if its most recent action hit a
surfaced API error.

**ALLOWANCE** — your live Claude subscription usage (the same numbers `/usage`
shows): 5-hour session + weekly gauges with reset times.

**Click a session** (mouse) for a detail popup: that session's own three charts,
effective tokens, the model in use, and the named subagents that ran in the last
hour (each with peak context + effective tokens). Press **`?`** for in-app help.

## Run

```bash
./claude-cache-monitor.py            # live dashboard (alt-screen)
./claude-cache-monitor.py --once     # render a single frame and exit
./claude-cache-monitor.py --interval 60   # override the 5-min data scan
```

Keys: `?` help · `↑/↓` `PgUp/PgDn` `j/k` scroll help · `q`/`esc` close overlay ·
click a session for detail · `Ctrl-C` to quit.

## How it works / requirements

- **Python 3, stdlib only** — no dependencies.
- A truecolour terminal ~152 columns wide (it adapts chart height to your terminal
  height; very narrow terminals will wrap).
- The **ALLOWANCE** panel calls `GET https://api.anthropic.com/api/oauth/usage` using
  the OAuth token Claude Code already stores in `~/.claude/.credentials.json`. The
  token is read fresh at call time and used only for that request — it is never
  logged, displayed, or persisted by this tool.

## Notes

- Empirically, Claude Code's **5-minute** ephemeral cache holds subagent/sidechain
  context and the **1-hour** cache holds the main thread.
- The script writes a local `claude-cache-monitor.log` (diagnostics only, no secrets);
  it's gitignored.
