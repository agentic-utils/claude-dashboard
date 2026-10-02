# Wheelhouse protocol

This session was launched from claude-wheelhouse, a sidecar that shows the person every
task, question and subagent status across their parallel Claude Code sessions. These
instructions cover only how to report to the wheelhouse. Your own instructions (CLAUDE.md
and the like) still decide how you work and what you say in chat; where anything here
seems to conflict with them, follow yours.

- Track work in the wheelhouse with the `wheelhouse` MCP tools. `post_item` creates a task
  (`T`), a question (`Q`) or a subagent status (`A`) and returns its ref, such as `Q3`. Put
  the full detail in `body` once; afterwards refer to it by ref. Keep `title` to a few
  words.
- Post your questions to the person with `post_item(kind="question")` as well as asking
  them in chat. The person reads them in the wheelhouse inbox, away from this chat, so the
  body must stand on its own: name the file, symbol or value, what's already decided, the
  options, and what each answer changes.
- Keep statuses current with `update_item`: tasks `todo running blocked waiting done
  dropped`; agents `running done failed`. Add a `note` for progress worth keeping.
- The person's answers and hints arrive as notifications from the wheelhouse monitor,
  marked `[wheelhouse]`. Treat them as if typed in chat. If a notification says it was cut
  short, call `get_input(ref)` for the full text.
- `/wheelhouse park` and `/wheelhouse end` handle the session's lifecycle (also
  `/wheelhouse:wheelhouse`). The wheelhouse may ask you to park or end, in a `[wheelhouse]`
  notification; do it as the notification says. If a later one says the request was
  cancelled, carry on as before.
