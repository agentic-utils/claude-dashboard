"""claude-board: the TUI by default, plus the per-session processes it launches."""

import argparse


def main() -> None:
    p = argparse.ArgumentParser(prog="claude-board", description="Sidecar board for parallel Claude Code sessions")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="the board (default)")
    run = sub.add_parser("run", help="inside a launched tab: exec Claude for a session")
    run.add_argument("session_id")
    sub.add_parser("mcp", help="the per-session MCP server (stdio)")
    sub.add_parser("monitor", help="the per-session plugin monitor")
    args = p.parse_args()

    if args.cmd == "run":
        from .launch import run as run_session
        run_session(args.session_id)
    elif args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
    elif args.cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main()
    else:
        from .tui import BoardApp
        BoardApp().run()
