# Session Manager

A local tool for browsing, filtering, and resuming Claude Code and Codex CLI sessions from your browser or terminal.

## Preview

The screenshot uses fictional demo sessions and contains no real conversation data.

![Session Manager demo showing the session list and conversation details](docs/snapshot.png)

## Usage

Run with Python 3; no third-party dependencies are required:

```sh
python3 session_manager.py                  # Open the local web app (default: 127.0.0.1:8765)
python3 session_manager.py list             # List sessions in the terminal
python3 session_manager.py pick             # Pick and resume a session interactively
python3 session_manager.py resume <session-id>  # Resume a specific session
```

The app reads local session data from `~/.claude/projects/` and `~/.codex/sessions/`. Do not commit session data or local configuration files to this repository.
