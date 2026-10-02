# Session board protocol

This session was launched from the session board, a sidecar that shows the person every
task, question and subagent status across their parallel Claude Code sessions. Use it
instead of restating things in chat.

- Track work on the board with the `board` MCP tools. `post_item` creates a task (`T`),
  a question (`Q`) or a subagent status (`A`) and returns its ref, such as `Q3`. Put the
  full detail in `body` once; afterwards refer to it only by ref. Keep `title` to a few
  words.
- Ask questions with `post_item(kind="question")`, not only in chat. The body must stand
  on its own: name the file, symbol or value, what's already decided, the options, and
  what each answer changes. In chat, a one-line pointer is enough ("Q3 is up").
- Keep statuses current with `update_item`: tasks `todo running blocked waiting done
  dropped`; agents `running done failed`. Add a `note` for progress worth keeping.
- Don't print a status board in chat. The board shows it.
- The person's answers and hints arrive as notifications from the board monitor, marked
  `[board]`. Act on them as if typed in chat. If a notification says it was cut short,
  call `get_input(ref)` for the full text.
- `/wheelhouse park` and `/wheelhouse end` handle the session's lifecycle (also `/board:wheelhouse`).
  The board may ask you to park or end, in a `[board]` notification; do it as the
  notification says. If a later one says the request was cancelled, carry on as before.
