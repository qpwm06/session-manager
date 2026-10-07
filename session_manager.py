#!/usr/bin/env python3
"""Session Manager for Claude Code & Codex CLI sessions."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_INDEX = HOME / ".codex" / "session_index.jsonl"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

_sessions_cache = []


def _resume_command_parts(source, session_id):
    """Return the correct CLI command parts for resuming a session."""
    if source == "claude":
        return ["claude", "--resume", session_id]
    if source == "codex":
        return ["codex", "resume", session_id]
    return []


def _resume_command(source, session_id):
    """Return the correct CLI command for resuming a session."""
    parts = _resume_command_parts(source, session_id)
    return shlex.join(parts) if parts else ""


def _truncate(text, width):
    """Trim text to a fixed width for terminal output."""
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[:width - 3] + "..."


def _open_path(path):
    """Open a folder path using the host platform's file browser."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        opener = shutil.which("xdg-open")
        if not opener:
            return False
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _server_url_host(host):
    """Return a browser-friendly host name for the local URL."""
    if host in ("", "0.0.0.0", "::"):
        return "localhost"
    return host


def _create_server(host, preferred_port, scan_ports):
    """Bind the HTTP server to a free port, scanning forward if needed."""
    host = host or ""
    if preferred_port == 0:
        server = HTTPServer((host, 0), Handler)
        return server, server.server_port, False

    attempts = max(scan_ports, 1)
    last_error = None
    for offset in range(attempts):
        port = preferred_port + offset
        try:
            server = HTTPServer((host, port), Handler)
            return server, server.server_port, offset != 0
        except OSError as exc:
            last_error = exc
    raise OSError(
        f"Could not bind to {host or '0.0.0.0'} ports "
        f"{preferred_port}-{preferred_port + attempts - 1}: {last_error}"
    )


def _sorted_sessions():
    """Return sessions sorted from most recent to oldest."""
    return sorted(_sessions_cache, key=lambda s: s.get("modified") or "", reverse=True)


def _filter_sessions(source="all", query=""):
    """Return sessions filtered for CLI usage."""
    query = (query or "").strip().lower()
    sessions = _sorted_sessions()
    if source != "all":
        sessions = [s for s in sessions if s["source"] == source]
    if query:
        sessions = [
            s for s in sessions
            if query in " ".join([
                s.get("id", ""),
                s.get("project", ""),
                s.get("summary", ""),
                s.get("folder_path", ""),
            ]).lower()
        ]
    return sessions


def _resolve_session(session_ref):
    """Resolve a session id, allowing unique prefix matches."""
    exact = [s for s in _sessions_cache if s["id"] == session_ref]
    if exact:
        return exact[0], []

    prefix_matches = [s for s in _sessions_cache if s["id"].startswith(session_ref)]
    if len(prefix_matches) == 1:
        return prefix_matches[0], []
    return None, prefix_matches


def _print_session_rows(sessions):
    """Print sessions in a compact terminal table."""
    if not sessions:
        print("No sessions found.")
        return

    print(f"{'#':>3}  {'SRC':<6}  {'MODIFIED':<20}  {'SESSION ID':<16}  {'PROJECT':<26}  SUMMARY")
    for index, session in enumerate(sessions, start=1):
        modified = _truncate(session.get("modified") or "-", 20)
        session_id = _truncate(session.get("id") or "-", 16)
        project = _truncate(session.get("project") or "-", 26)
        summary = _truncate(session.get("summary") or "-", 80)
        print(
            f"{index:>3}  {session['source']:<6}  {modified:<20}  "
            f"{session_id:<16}  {project:<26}  {summary}"
        )


def _exec_resume(session, dry_run=False):
    """Resume a session in the current terminal."""
    cmd = _resume_command_parts(session["source"], session["id"])
    if not cmd:
        print(f"Unsupported session source: {session['source']}", file=sys.stderr)
        return 1

    executable = shutil.which(cmd[0])
    if not executable:
        print(f"Command not found in PATH: {cmd[0]}", file=sys.stderr)
        return 1

    cwd = session.get("folder_path") or os.getcwd()
    if cwd and not os.path.isdir(cwd):
        cwd = os.getcwd()

    printable = shlex.join(cmd)
    if dry_run:
        print(printable)
        return 0

    print(f"Resuming {session['source']} session {session['id']} in {cwd}")
    os.chdir(cwd)
    os.execvp(executable, cmd)
    return 0


def _command_list(args):
    scan_all()
    sessions = _filter_sessions(source=args.source, query=args.query)
    if args.limit > 0:
        sessions = sessions[:args.limit]
    _print_session_rows(sessions)
    return 0


def _command_resume(args):
    scan_all()
    session, matches = _resolve_session(args.session_id)
    if not session:
        if matches:
            print(f"Session id prefix '{args.session_id}' is ambiguous.", file=sys.stderr)
            _print_session_rows(matches[:10])
        else:
            print(f"Session '{args.session_id}' not found.", file=sys.stderr)
        return 1
    return _exec_resume(session, dry_run=args.dry_run)


def _command_pick(args):
    scan_all()
    sessions = _filter_sessions(source=args.source, query=args.query)
    if args.limit > 0:
        sessions = sessions[:args.limit]
    _print_session_rows(sessions)
    if not sessions:
        return 1
    if not sys.stdin.isatty():
        print("Interactive pick requires a terminal.", file=sys.stderr)
        return 1

    choice = input("Resume which session? Enter index or session id prefix: ").strip()
    if not choice:
        print("Cancelled.")
        return 0

    if choice.isdigit():
        index = int(choice)
        if index < 1 or index > len(sessions):
            print(f"Invalid selection: {choice}", file=sys.stderr)
            return 1
        return _exec_resume(sessions[index - 1], dry_run=args.dry_run)

    session, matches = _resolve_session(choice)
    if not session:
        if matches:
            print(f"Session id prefix '{choice}' is ambiguous.", file=sys.stderr)
            _print_session_rows(matches[:10])
        else:
            print(f"Session '{choice}' not found.", file=sys.stderr)
        return 1
    return _exec_resume(session, dry_run=args.dry_run)


def _command_serve(args):
    print("Scanning sessions...")
    scan_all()
    print(f"Found {len(_sessions_cache)} sessions")

    server, port, port_changed = _create_server(args.host, args.port, args.scan_ports)
    url = f"http://{_server_url_host(args.host)}:{port}"
    if port_changed:
        print(f"Port {args.port} is busy, switched to {port}")
    print(f"Starting server at {url}")

    if not args.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def _parse_args(argv):
    """Parse CLI arguments while keeping legacy `python session_manager.py --no-open` usage."""
    if not argv:
        argv = ["serve"]
    elif argv[0].startswith("-") and argv[0] not in {"-h", "--help"}:
        argv = ["serve", *argv]

    parser = argparse.ArgumentParser(
        description="Manage Claude Code and Codex sessions from a browser or terminal."
    )
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the web session manager.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765, help="Preferred port. Use 0 for auto.")
    serve_parser.add_argument(
        "--scan-ports",
        type=int,
        default=20,
        help="How many ports to scan forward when the preferred port is busy.",
    )
    serve_parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")

    list_parser = subparsers.add_parser("list", help="List sessions in the terminal.")
    list_parser.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    list_parser.add_argument("--query", default="", help="Filter by id, project, summary, or cwd.")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum rows to print. 0 means all.")

    resume_parser = subparsers.add_parser("resume", help="Resume a session directly in the terminal.")
    resume_parser.add_argument("session_id", help="Exact session id or unique prefix.")
    resume_parser.add_argument("--dry-run", action="store_true", help="Print the resume command without executing it.")

    start_parser = subparsers.add_parser("start", help="Alias of `resume` for terminal startup.")
    start_parser.add_argument("session_id", help="Exact session id or unique prefix.")
    start_parser.add_argument("--dry-run", action="store_true", help="Print the resume command without executing it.")

    pick_parser = subparsers.add_parser("pick", help="Pick a session from a terminal list and resume it.")
    pick_parser.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    pick_parser.add_argument("--query", default="", help="Filter by id, project, summary, or cwd.")
    pick_parser.add_argument("--limit", type=int, default=20, help="Maximum rows to show. 0 means all.")
    pick_parser.add_argument("--dry-run", action="store_true", help="Print the resume command without executing it.")

    return parser.parse_args(argv)


def scan_claude_sessions():
    """Scan all Claude Code sessions from ~/.claude/projects/."""
    sessions = []
    if not CLAUDE_PROJECTS.is_dir():
        return sessions

    for project_dir in CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name.replace("-", "/")
        index_file = project_dir / "sessions-index.json"
        indexed_ids = set()

        # Try sessions-index.json first
        if index_file.exists():
            try:
                data = json.loads(index_file.read_text())
                for entry in data.get("entries", []):
                    sid = entry.get("sessionId", "")
                    indexed_ids.add(sid)
                    fp = entry.get("fullPath", "")
                    readable = bool(fp and os.path.isfile(fp))
                    # Try to get cwd from the session file
                    cwd = _quick_cwd(fp) if readable else ""
                    sessions.append({
                        "id": sid,
                        "source": "claude",
                        "project": project_name,
                        "summary": (entry.get("firstPrompt") or "")[:120],
                        "message_count": entry.get("messageCount", ""),
                        "modified": _epoch_ms_to_iso(entry.get("fileMtime", 0)),
                        "file_path": fp,
                        "folder_path": cwd,
                        "index_path": str(index_file),
                        "readable": readable,
                    })
            except Exception:
                pass

        # Also pick up .jsonl files not in the index
        for jsonl in project_dir.glob("*.jsonl"):
            sid = jsonl.stem
            if sid in indexed_ids:
                continue
            info = _parse_claude_jsonl(jsonl)
            sessions.append({
                "id": sid,
                "source": "claude",
                "project": project_name,
                "summary": info.get("summary", ""),
                "message_count": info.get("message_count", ""),
                "modified": info.get("modified", ""),
                "file_path": str(jsonl),
                "folder_path": info.get("cwd") or str(project_dir),
                "index_path": "",
                "readable": True,
            })
    return sessions


def _quick_cwd(path):
    """Extract cwd from the first few lines of a Claude .jsonl."""
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 10:
                    break
                obj = json.loads(line)
                if obj.get("cwd"):
                    return obj["cwd"]
    except Exception:
        pass
    return ""


def _parse_claude_jsonl(path):
    """Lightweight parse of a Claude .jsonl to extract summary info."""
    info = {"summary": "", "message_count": "", "modified": "", "cwd": ""}
    try:
        mtime = os.path.getmtime(path)
        info["modified"] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = 0
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "user":
                    count += 1
                    if not info["cwd"] and obj.get("cwd"):
                        info["cwd"] = obj["cwd"]
                    if not info["summary"]:
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            info["summary"] = content[:120]
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    info["summary"] = part.get("text", "")[:120]
                                    break
                elif obj.get("type") == "assistant":
                    count += 1
        info["message_count"] = count if count else ""
    except Exception:
        pass
    return info


def _epoch_ms_to_iso(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def scan_codex_sessions():
    """Scan Codex sessions from ~/.codex/."""
    sessions = []
    index_map = {}
    if CODEX_INDEX.exists():
        try:
            for line in CODEX_INDEX.read_text().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                index_map[entry["id"]] = entry
        except Exception:
            pass

    if not CODEX_SESSIONS.is_dir():
        return sessions

    for jsonl in CODEX_SESSIONS.rglob("*.jsonl"):
        info = _parse_codex_jsonl(jsonl)
        sid = info.get("id") or jsonl.stem
        idx = index_map.pop(sid, {})
        sessions.append({
            "id": sid,
            "source": "codex",
            "project": idx.get("thread_name", info.get("cwd", "")),
            "summary": info.get("user_msg") or info.get("cwd", ""),
            "message_count": info.get("msg_count") or "",
            "modified": idx.get("updated_at", info.get("timestamp", "")),
            "file_path": str(jsonl),
            "folder_path": info.get("cwd", ""),
            "index_path": str(CODEX_INDEX) if CODEX_INDEX.exists() else "",
            "readable": True,
        })
    return sessions


def _parse_codex_jsonl(path):
    """Parse Codex session file for metadata, first user message, and message count."""
    info = {"msg_count": 0}
    response_count = 0
    response_first = ""
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "session_meta":
                    payload = obj.get("payload", {})
                    info["id"] = payload.get("id", "")
                    info["cwd"] = payload.get("cwd", "")
                    info["timestamp"] = payload.get("timestamp", obj.get("timestamp", ""))
                elif obj.get("type") == "event_msg":
                    payload = obj.get("payload", {})
                    if payload.get("type") == "user_message":
                        info["msg_count"] += 1
                        if not info.get("user_msg"):
                            info["user_msg"] = (payload.get("message") or "")[:120]
                elif obj.get("type") == "response_item":
                    message = _codex_response_message(obj.get("payload", {}))
                    if message and message["role"] == "user":
                        response_count += 1
                        if not response_first:
                            response_first = message["text"][:120]
    except Exception:
        pass
    # 新版日志以 response_item 保存对话；旧版 event_msg 可能同时存在，避免重复计数。
    if response_count:
        info["msg_count"] = response_count
        info["user_msg"] = response_first
    return info


def _codex_response_message(payload):
    """Extract a user or assistant text message from a Codex response item."""
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    content = payload.get("content", [])
    if not isinstance(content, list):
        return None
    parts = [part.get("text", "") for part in content
             if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")
             and isinstance(part.get("text"), str)]
    text = "\n".join(part for part in parts if part)
    return {"role": role, "text": text} if text else None


def scan_all():
    global _sessions_cache
    _sessions_cache = scan_claude_sessions() + scan_codex_sessions()
    return _sessions_cache


def load_messages(session_id):
    """Load conversation messages for a session."""
    session = None
    for s in _sessions_cache:
        if s["id"] == session_id:
            session = s
            break
    if not session or not session.get("readable"):
        return {"error": "Session not found or unreadable"}

    fp = session["file_path"]
    if not fp or not os.path.isfile(fp):
        return {"error": "File not found"}

    messages = []
    resume_cmd = _resume_command(session["source"], session["id"])
    if session["source"] == "claude":
        try:
            with open(fp, "r", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "user":
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        text = _extract_text(content)
                        if text:
                            messages.append({"role": "user", "text": text})
                    elif obj.get("type") == "assistant":
                        msg = obj.get("message", {})
                        content = msg.get("content", [])
                        text = _extract_text(content)
                        if text:
                            messages.append({"role": "assistant", "text": text})
        except Exception:
            pass
    elif session["source"] == "codex":
        legacy_messages = []
        response_messages = []
        try:
            with open(fp, "r", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "response_item":
                        message = _codex_response_message(obj.get("payload", {}))
                        if message:
                            response_messages.append(message)
                    if obj.get("type") == "event_msg":
                        payload = obj.get("payload", {})
                        if payload.get("type") == "user_message":
                            text = payload.get("message", "")
                            if text:
                                legacy_messages.append({"role": "user", "text": text})
                        elif payload.get("type") == "agent_message":
                            text = payload.get("message", "")
                            if text:
                                legacy_messages.append({"role": "assistant", "text": text})
        except Exception:
            pass
        # 两种记录同时存在时优先使用结构化消息，避免同一轮对话显示两遍。
        messages = response_messages or legacy_messages

    return {
        "id": session["id"],
        "source": session["source"],
        "project": session["project"],
        "resume_cmd": resume_cmd,
        "folder_path": session.get("folder_path", ""),
        "modified": session.get("modified", ""),
        "message_count": session.get("message_count", ""),
        "messages": messages,
    }


def _extract_text(content):
    """Extract plain text from Claude message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join(parts)
    return ""


def delete_sessions(ids_to_delete):
    """Delete sessions by id. Returns count deleted."""
    id_set = set(ids_to_delete)
    deleted = 0
    index_updates = {}

    for s in list(_sessions_cache):
        if s["id"] not in id_set:
            continue
        fp = s["file_path"]
        if fp and os.path.exists(fp):
            os.remove(fp)
            deleted += 1
        if s.get("index_path"):
            index_updates.setdefault(s["index_path"], set()).add(s["id"])

    for idx_path, removed_ids in index_updates.items():
        if idx_path.endswith("sessions-index.json") and os.path.exists(idx_path):
            try:
                data = json.loads(Path(idx_path).read_text())
                data["entries"] = [e for e in data.get("entries", [])
                                   if e.get("sessionId") not in removed_ids]
                Path(idx_path).write_text(json.dumps(data, indent=2))
            except Exception:
                pass
        elif idx_path.endswith("session_index.jsonl") and os.path.exists(idx_path):
            try:
                lines = Path(idx_path).read_text().splitlines()
                kept = [l for l in lines if l.strip() and
                        json.loads(l).get("id") not in removed_ids]
                Path(idx_path).write_text("\n".join(kept) + "\n" if kept else "")
            except Exception:
                pass

    scan_all()
    return deleted


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f4f4f1;--side:#ecebe7;--panel:#ffffff;--raised:#f8f8f6;
  --hover:rgba(20,20,30,.045);--sel:rgba(74,91,216,.07);
  --border:#e3e2dd;--border2:#d0cfc9;
  --text:#1b1c20;--text2:#585b64;--text3:#8d9099;
  --accent:#4a5bd8;--accent-fg:#ffffff;--accent-soft:rgba(74,91,216,.11);
  --claude:#c0603c;--claude-soft:rgba(192,96,60,.11);
  --codex:#16866a;--codex-soft:rgba(22,134,106,.11);
  --danger:#d14343;--danger-soft:rgba(209,67,67,.1);
  --shadow:0 12px 40px rgba(20,20,30,.16),0 2px 6px rgba(20,20,30,.06);
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  --r:8px;--r-sm:6px;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#111215;--side:#0c0d0f;--panel:#16171b;--raised:#1c1d22;
    --hover:rgba(255,255,255,.045);--sel:rgba(139,156,255,.08);
    --border:#25272d;--border2:#353840;
    --text:#e8e9ed;--text2:#a3a6b0;--text3:#6d717c;
    --accent:#8b9cff;--accent-fg:#0f1020;--accent-soft:rgba(139,156,255,.14);
    --claude:#e58a65;--claude-soft:rgba(229,138,101,.13);
    --codex:#4cc7a2;--codex-soft:rgba(76,199,162,.12);
    --danger:#f06868;--danger-soft:rgba(240,104,104,.12);
    --shadow:0 16px 50px rgba(0,0,0,.5),0 2px 8px rgba(0,0,0,.3);
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#111215;--side:#0c0d0f;--panel:#16171b;--raised:#1c1d22;
  --hover:rgba(255,255,255,.045);--sel:rgba(139,156,255,.08);
  --border:#25272d;--border2:#353840;
  --text:#e8e9ed;--text2:#a3a6b0;--text3:#6d717c;
  --accent:#8b9cff;--accent-fg:#0f1020;--accent-soft:rgba(139,156,255,.14);
  --claude:#e58a65;--claude-soft:rgba(229,138,101,.13);
  --codex:#4cc7a2;--codex-soft:rgba(76,199,162,.12);
  --danger:#f06868;--danger-soft:rgba(240,104,104,.12);
  --shadow:0 16px 50px rgba(0,0,0,.5),0 2px 8px rgba(0,0,0,.3);
  color-scheme:dark;
}
html,body{height:100%}
body{font:13px/1.45 var(--sans);background:var(--bg);color:var(--text);overflow:hidden;
  -webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
svg{display:block}
[hidden]{display:none!important}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:10px;border:3px solid transparent;background-clip:content-box}
::-webkit-scrollbar-track{background:transparent}
kbd{font-family:var(--mono);font-size:10.5px;padding:0 5px;line-height:16px;border:1px solid var(--border2);
  border-bottom-width:2px;border-radius:4px;color:var(--text3);background:var(--panel)}

.app{display:grid;grid-template-columns:240px minmax(0,1fr);height:100vh}

/* Sidebar */
.side{background:var(--side);border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:10px;padding:16px 18px 10px}
.logo{width:28px;height:28px;border-radius:8px;background:var(--text);color:var(--bg);display:grid;place-items:center;flex-shrink:0}
.logo svg{width:16px;height:16px}
.brand-name{font-weight:650;font-size:14px;letter-spacing:-.01em}
.brand-sub{font-size:11.5px;color:var(--text3)}
.side-scroll{flex:1;overflow-y:auto;padding:0 10px 12px}
.side-sec{padding:12px 0 2px}
.side-label{font-size:11px;font-weight:600;color:var(--text3);padding:0 8px 6px;letter-spacing:.02em;
  display:flex;align-items:center}
.side-label button{margin-left:auto;font-size:11px;font-weight:500;color:var(--text3)}
.side-label button:hover{color:var(--text)}
.nav{display:flex;align-items:center;gap:9px;width:100%;padding:6px 8px;border-radius:var(--r-sm);
  color:var(--text2);text-align:left}
.nav:hover{background:var(--hover);color:var(--text)}
.nav.on{background:var(--panel);color:var(--text);font-weight:550;box-shadow:0 0 0 1px var(--border)}
.nav .label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav .n{margin-left:auto;font-size:11.5px;color:var(--text3);font-variant-numeric:tabular-nums;font-weight:400}
.nav.proj .label{font-family:var(--mono);font-size:11.5px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:var(--text3)}
.dot.claude{background:var(--claude)}
.dot.codex{background:var(--codex)}
.dot.all{background:conic-gradient(var(--claude) 0 50%,var(--codex) 0)}
.chips-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
.chip{padding:5px 0;border-radius:var(--r-sm);font-size:12px;color:var(--text2);border:1px solid transparent}
.chip:hover{background:var(--hover);color:var(--text)}
.chip.on{background:var(--panel);color:var(--text);border-color:var(--border);font-weight:550}
.ctl{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-sm);color:var(--text2);cursor:pointer}
.ctl:hover{background:var(--hover);color:var(--text)}
.ctl .grow{flex:1}
.ctl .n{font-size:11.5px;color:var(--text3);font-variant-numeric:tabular-nums}
.num{width:58px;height:24px;padding:0 6px;border:1px solid var(--border);background:var(--panel);border-radius:5px;
  font-size:12px;text-align:right;outline:none}
.num:focus{border-color:var(--accent)}
.switch{appearance:none;-webkit-appearance:none;width:28px;height:16px;border-radius:99px;background:var(--border2);
  position:relative;cursor:pointer;flex-shrink:0;transition:background .15s}
.switch::before{content:'';position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;
  background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2);transition:transform .15s}
.switch:checked{background:var(--accent)}
.switch:checked::before{transform:translateX(12px)}
.side-foot{border-top:1px solid var(--border);padding:8px 10px;display:flex;align-items:center;gap:4px}
.side-foot .ctl{flex:1}
.empty-mini{padding:4px 8px;color:var(--text3);font-size:12px}

/* Main */
.main{display:flex;min-width:0;min-height:0}
.listpane{flex:1;min-width:340px;display:flex;flex-direction:column;min-height:0;background:var(--panel)}
.list-head{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--border)}
.search{flex:1;position:relative;max-width:560px}
.search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);width:15px;height:15px;color:var(--text3);pointer-events:none}
.search input{width:100%;height:32px;padding:0 34px 0 32px;border:1px solid var(--border);background:var(--raised);
  border-radius:var(--r);outline:none;transition:border-color .15s,box-shadow .15s,background .15s}
.search input::placeholder{color:var(--text3)}
.search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);background:var(--panel)}
.search kbd{position:absolute;right:9px;top:50%;transform:translateY(-50%)}
.sel-wrap{position:relative;flex-shrink:0}
.sel-wrap select{appearance:none;-webkit-appearance:none;height:32px;padding:0 28px 0 10px;border:1px solid var(--border);
  border-radius:var(--r);background:var(--raised);cursor:pointer;outline:none;color:var(--text2)}
.sel-wrap select:hover{color:var(--text);border-color:var(--border2)}
.sel-wrap select:focus{border-color:var(--accent)}
.sel-wrap svg{position:absolute;right:9px;top:50%;transform:translateY(-50%);width:12px;height:12px;pointer-events:none;color:var(--text3)}
.icon-btn{width:30px;height:30px;display:inline-grid;place-items:center;border-radius:var(--r-sm);color:var(--text2);flex-shrink:0}
.icon-btn:hover{background:var(--hover);color:var(--text)}
.icon-btn svg{width:16px;height:16px}
.icon-btn.spin svg{animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.only-mobile{display:none}

.list-bar{display:flex;align-items:center;gap:10px;padding:0 16px 0 18px;border-bottom:1px solid var(--border);
  height:42px;font-size:12px;color:var(--text2);flex-shrink:0}
.list-bar .count{font-variant-numeric:tabular-nums;white-space:nowrap}
.list-bar .count b{color:var(--text);font-weight:600}
.fchips{display:flex;gap:5px;min-width:0;overflow:hidden}
.fchip{display:inline-flex;align-items:center;gap:3px;padding:0 3px 0 8px;height:22px;border-radius:99px;
  background:var(--accent-soft);color:var(--accent);font-size:11.5px;font-weight:500;white-space:nowrap;max-width:220px}
.fchip span{overflow:hidden;text-overflow:ellipsis}
.fchip button{width:16px;height:16px;border-radius:50%;display:grid;place-items:center;flex-shrink:0}
.fchip button:hover{background:var(--accent-soft)}
.fchip svg{width:10px;height:10px}
.linkish{color:var(--text3);font-size:11.5px;white-space:nowrap}
.linkish:hover{color:var(--text)}
.spacer{flex:1}
.bulk{display:flex;align-items:center;gap:6px;white-space:nowrap}
.bulk b{color:var(--text);font-weight:600}
.btn{height:28px;padding:0 12px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--panel);
  font-size:12.5px;font-weight:500;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn:hover{background:var(--raised);border-color:var(--border2)}
.btn svg{width:13px;height:13px}
.btn.sm{height:24px;padding:0 9px;font-size:12px}
.btn.ghost{border-color:transparent;background:none;color:var(--text2)}
.btn.ghost:hover{background:var(--hover);color:var(--text)}
.btn.danger{background:var(--danger);border-color:var(--danger);color:#fff}
.btn.danger:hover{filter:brightness(1.08)}
.btn:disabled{opacity:.5;cursor:default}

.list{flex:1;overflow-y:auto;padding:0 8px 28px}
.group{position:sticky;top:0;z-index:1;background:var(--panel);padding:14px 10px 6px;font-size:11px;
  font-weight:600;color:var(--text3);display:flex;gap:6px;letter-spacing:.02em}
.group .gn{font-weight:400}
.row{position:relative;display:flex;align-items:center;gap:8px;padding:9px 8px;border-radius:var(--r);cursor:pointer;
  scroll-margin:40px 0 8px}
.row:hover{background:var(--hover)}
.row.selected{background:var(--sel)}
.row.active{background:var(--accent-soft)}
.row.active::before{content:'';position:absolute;left:0;top:10px;bottom:10px;width:2px;border-radius:2px;background:var(--accent)}
.check{display:grid;place-items:center;width:20px;height:20px;flex-shrink:0;opacity:0;transition:opacity .1s;cursor:pointer}
.row:hover .check,.row.selected .check,body.selecting .check{opacity:1}
input[type=checkbox]:not(.switch){accent-color:var(--accent);width:14px;height:14px;cursor:pointer}
.row-main{flex:1;min-width:0}
.row-title{font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row-meta{display:flex;align-items:center;gap:8px;margin-top:3px;font-size:12px;color:var(--text3);min-width:0}
.src{display:inline-flex;align-items:center;gap:5px;font-weight:550;flex-shrink:0;font-size:11.5px}
.src::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}
.src.claude{color:var(--claude)}
.src.codex{color:var(--codex)}
.loc{font-family:var(--mono);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.loc b{font-weight:500;color:var(--text2)}
.badge{font-size:10.5px;font-weight:600;padding:1px 6px;border-radius:4px;flex-shrink:0}
.badge.danger{background:var(--danger-soft);color:var(--danger)}
.row.lost .row-title{color:var(--text2)}
.muted{color:var(--text3)}
.row-side{display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0;font-size:11.5px;
  color:var(--text3);font-variant-numeric:tabular-nums;min-width:58px}
.row-side time{color:var(--text2)}
.row-open{visibility:hidden}
.row:hover .row-open{visibility:visible}
.row-gap{width:30px;flex-shrink:0}
.list-empty{text-align:center;color:var(--text3);padding:80px 20px}
.list-empty .t{color:var(--text2);font-size:14px;font-weight:550;margin-bottom:4px}
.list-empty .btn{margin-top:14px}

/* Detail */
.resizer{width:5px;margin:0 -2px;cursor:col-resize;z-index:2;position:relative;flex-shrink:0}
.resizer::after{content:'';position:absolute;top:0;bottom:0;left:2px;width:1px;background:var(--border);transition:background .15s}
.resizer:hover::after,.resizer.active::after{background:var(--accent);left:1px;width:3px}
.detail{width:480px;min-width:320px;display:flex;flex-direction:column;min-height:0;background:var(--bg)}
.d-empty{margin:auto;text-align:center;color:var(--text3);padding:24px}
.d-empty svg{width:36px;height:36px;margin:0 auto 14px;opacity:.55}
.d-empty .t{color:var(--text2);font-weight:550;font-size:14px;margin-bottom:4px}
.keys{display:grid;grid-template-columns:auto auto;gap:8px 12px;margin-top:22px;justify-content:center;font-size:12px;text-align:left;align-items:center}
.keys dt{text-align:right}
.d-scroll{flex:1;overflow-y:auto}
.d-top{display:flex;align-items:center;gap:6px;padding:12px 12px 0 20px}
.pill{font-size:11px;font-weight:600;padding:3px 9px;border-radius:99px;display:inline-flex;align-items:center;gap:6px}
.pill::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}
.pill.claude{background:var(--claude-soft);color:var(--claude)}
.pill.codex{background:var(--codex-soft);color:var(--codex)}
.d-head{padding:12px 20px 20px;border-bottom:1px solid var(--border)}
.d-title{font-size:17px;font-weight:650;letter-spacing:-.01em;line-height:1.35;word-break:break-word;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.d-meta{display:grid;grid-template-columns:76px minmax(0,1fr);gap:8px 12px;margin-top:16px;font-size:12.5px}
.d-meta dt{color:var(--text3)}
.d-meta dd{color:var(--text2);min-width:0;display:flex;align-items:center;gap:6px}
.d-meta .mono{font-family:var(--mono);font-size:11.5px;word-break:break-all}
.mini{font-size:11px;color:var(--text3);padding:1px 6px;border-radius:4px;flex-shrink:0}
.mini:hover{background:var(--hover);color:var(--text)}
.cmd{margin-top:18px;border:1px solid var(--border);background:var(--panel);border-radius:var(--r);overflow:hidden}
.cmd-line{display:flex;gap:8px;padding:11px 12px;font-family:var(--mono);font-size:12px;align-items:baseline}
.cmd-line .p{color:var(--text3);user-select:none}
.cmd-line code{font-family:inherit;word-break:break-all;flex:1}
.cmd-actions{display:flex;gap:4px;padding:6px;border-top:1px solid var(--border);background:var(--raised)}
.d-tabs{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:10px;padding:10px 20px;
  background:var(--bg);border-bottom:1px solid var(--border)}
.d-tabs .label{font-size:12px;color:var(--text3);font-weight:600}
.seg{display:inline-flex;padding:2px;background:var(--hover);border-radius:7px;margin-left:auto}
.seg button{padding:2px 10px;border-radius:5px;font-size:12px;color:var(--text2)}
.seg button.on{background:var(--panel);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.08),0 0 0 1px var(--border)}
.seg .c{color:var(--text3);margin-left:4px;font-variant-numeric:tabular-nums}
.transcript{padding:18px 20px 48px;display:flex;flex-direction:column;gap:18px}
.msg header{display:flex;align-items:center;gap:8px;font-size:11.5px;margin-bottom:6px;color:var(--text3)}
.msg .who{font-weight:600;color:var(--text2)}
.msg.user .who{color:var(--accent)}
.msg header .mini{margin-left:auto;opacity:0}
.msg:hover header .mini,.msg header .mini:focus-visible{opacity:1}
.msg .body{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.62}
.msg.user .body{background:var(--panel);border:1px solid var(--border);border-left:2px solid var(--accent);
  border-radius:var(--r);padding:10px 12px}
.msg.assistant .body{padding:0 2px}
.msg.collapsed .body{max-height:240px;overflow:hidden;
  -webkit-mask-image:linear-gradient(#000 65%,transparent);mask-image:linear-gradient(#000 65%,transparent)}
.more{margin-top:6px;font-size:12px;color:var(--accent);font-weight:500}
.more:hover{text-decoration:underline}
.d-note{padding:40px 20px;text-align:center;color:var(--text3)}
.d-note.err{color:var(--danger)}
.sk{height:10px;border-radius:4px;background:var(--hover);animation:pulse 1.2s ease-in-out infinite;margin-bottom:10px}
@keyframes pulse{50%{opacity:.4}}

.toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,8px);background:var(--text);color:var(--bg);
  padding:8px 14px;border-radius:8px;font-size:12.5px;box-shadow:var(--shadow);opacity:0;
  transition:opacity .18s,transform .18s;pointer-events:none;z-index:60;max-width:calc(100vw - 32px)}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.error{background:var(--danger);color:#fff}

dialog{margin:auto;border:1px solid var(--border);border-radius:12px;background:var(--panel);color:var(--text);
  box-shadow:var(--shadow);width:min(440px,calc(100vw - 32px));padding:0}
dialog::backdrop{background:rgba(10,10,15,.42)}
.dlg-body{padding:20px 20px 4px}
.dlg-body h3{font-size:15px;font-weight:650;margin-bottom:6px}
.dlg-body p{color:var(--text2)}
.dlg-list{margin-top:12px;border:1px solid var(--border);border-radius:var(--r);max-height:180px;overflow-y:auto;
  list-style:none;font-size:12.5px}
.dlg-list li{padding:7px 10px;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text2)}
.dlg-list li:last-child{border-bottom:0}
.dlg-foot{display:flex;justify-content:flex-end;gap:8px;padding:16px 20px 18px}
.scrim{display:none}

@media (max-width:860px){
  .app{grid-template-columns:1fr}
  .side{position:fixed;top:0;bottom:0;left:0;width:min(280px,85vw);z-index:40;transform:translateX(-100%);
    transition:transform .2s;box-shadow:var(--shadow)}
  body.side-open .side{transform:none}
  .scrim{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:35}
  body.side-open .scrim{display:block}
  .only-mobile{display:inline-grid}
  .resizer{display:none}
  .listpane{min-width:0}
  .search kbd{display:none}
  .sel-wrap select{max-width:112px}
  .list-head{gap:6px}
  .detail{position:fixed;inset:0;width:auto!important;min-width:0;z-index:30;transform:translateX(100%);transition:transform .2s}
  body.detail-open .detail{transform:none}
  .d-top{padding-left:16px}
  .d-head,.d-tabs,.transcript{padding-left:16px;padding-right:16px}
  .row-open{display:none}
}
</style>
</head>
<body>
<div class="app">
  <aside class="side" id="side">
    <div class="brand">
      <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 8 4 4-4 4"/><path d="M12 16h7"/></svg></div>
      <div>
        <div class="brand-name">Sessions</div>
        <div class="brand-sub">Claude Code &middot; Codex</div>
      </div>
    </div>
    <div class="side-scroll">
      <nav class="side-sec">
        <div class="side-label">Source</div>
        <button class="nav" data-source="all"><span class="dot all"></span><span class="label">All sessions</span><span class="n"></span></button>
        <button class="nav" data-source="claude"><span class="dot claude"></span><span class="label">Claude Code</span><span class="n"></span></button>
        <button class="nav" data-source="codex"><span class="dot codex"></span><span class="label">Codex</span><span class="n"></span></button>
      </nav>
      <div class="side-sec">
        <div class="side-label">Updated within</div>
        <div class="chips-grid">
          <button class="chip" data-time="all">Any</button>
          <button class="chip" data-time="1h">1h</button>
          <button class="chip" data-time="6h">6h</button>
          <button class="chip" data-time="1d">24h</button>
          <button class="chip" data-time="3d">3d</button>
          <button class="chip" data-time="7d">7d</button>
          <button class="chip" data-time="30d">30d</button>
        </div>
      </div>
      <div class="side-sec">
        <div class="side-label">Filters</div>
        <label class="ctl" title="Only show sessions whose file is missing">
          <span class="grow">Missing files only</span><span class="n" id="lostCount"></span>
          <input type="checkbox" class="switch" id="lostToggle">
        </label>
        <label class="ctl" title="Show sessions with at most N messages (empty = off)">
          <span class="grow">Max messages</span>
          <input type="number" class="num" id="maxMsgs" min="0" placeholder="Off">
        </label>
      </div>
      <div class="side-sec">
        <div class="side-label">Directories<button id="projToggle" hidden></button></div>
        <div id="projects"></div>
      </div>
    </div>
    <div class="side-foot">
      <label class="ctl" title="Rescan every 30 seconds">
        <span class="grow">Auto-refresh</span>
        <input type="checkbox" class="switch" id="autoRefresh">
      </label>
      <button class="icon-btn" id="themeBtn" title="Theme"></button>
    </div>
  </aside>
  <div class="scrim" id="scrim"></div>

  <div class="main">
    <section class="listpane">
      <header class="list-head">
        <button class="icon-btn only-mobile" id="menuBtn" title="Filters">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
        </button>
        <div class="search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
          <input type="text" id="search" placeholder="Search prompts, directories, IDs" autocomplete="off" spellcheck="false">
          <kbd>/</kbd>
        </div>
        <div class="sel-wrap">
          <select id="sort" title="Sort">
            <option value="modified-desc">Newest first</option>
            <option value="modified-asc">Oldest first</option>
            <option value="msgs-desc">Most messages</option>
            <option value="project-asc">By directory</option>
          </select>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 9 6 6 6-6"/></svg>
        </div>
        <button class="icon-btn" id="refreshBtn" title="Rescan sessions">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 4v5h-5"/></svg>
        </button>
      </header>
      <div class="list-bar">
        <input type="checkbox" id="selectAll" title="Select all shown">
        <span class="count" id="countLabel"></span>
        <div class="fchips" id="fchips"></div>
        <div class="spacer"></div>
        <div class="bulk" id="bulk" hidden>
          <span><b id="selCount">0</b> selected</span>
          <button class="btn sm ghost" id="clearSel">Clear</button>
          <button class="btn sm danger" id="deleteBtn">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>
            Delete
          </button>
        </div>
      </div>
      <div class="list" id="list" role="listbox" aria-label="Sessions"></div>
    </section>
    <div class="resizer" id="resizer"></div>
    <section class="detail" id="detail"></section>
  </div>
</div>

<dialog id="confirmDlg">
  <form method="dialog">
    <div class="dlg-body">
      <h3 id="dlgTitle">Delete sessions?</h3>
      <p>The session files will be removed from disk. This cannot be undone.</p>
      <ul class="dlg-list" id="dlgList"></ul>
    </div>
    <div class="dlg-foot">
      <button class="btn" value="cancel">Cancel</button>
      <button class="btn danger" value="ok" id="dlgOk">Delete</button>
    </div>
  </form>
</dialog>
<div class="toast" id="toast"></div>
<script>
PLACEHOLDER_SCRIPT
</script>
</body>
</html>"""

JS_CODE = r"""
const $=id=>document.getElementById(id);
const store={
  get(k,d){try{const v=localStorage.getItem('sm.'+k);return v===null?d:v;}catch(e){return d;}},
  set(k,v){try{localStorage.setItem('sm.'+k,v);}catch(e){}}
};
const state={
  sessions:[], selected:new Set(), activeId:null, detail:null,
  sort:store.get('sort','modified-desc'), source:'all', time:'all', lost:false, maxMsgs:0,
  project:null, query:'', role:'all', allProjects:false
};
let visible=[], autoTimer=null, detailToken=0, detailTimer=null, toastTimer=null;

const SRC={claude:'Claude Code',codex:'Codex'};
const P='viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
const ICON={
  folder:`<svg ${P}><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  x:`<svg ${P}><path d="M18 6 6 18M6 6l12 12"/></svg>`,
  copy:`<svg ${P}><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>`,
  back:`<svg ${P}><path d="m15 18-6-6 6-6"/></svg>`,
  chat:`<svg ${P} stroke-width="1.4"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l.9-5.4A8 8 0 1 1 21 12z"/></svg>`,
  sun:`<svg ${P}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`,
  moon:`<svg ${P}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`,
  system:`<svg ${P}><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>`
};

/* ---------- helpers ---------- */
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function oneLine(s){return String(s||'').replace(/<[^>]{1,80}>/g,' ').replace(/\s+/g,' ').trim();}
function shortPath(p){return String(p||'').replace(/^\/(Users|home)\/[^/]+(?=\/|$)/,'~');}
function pathHtml(p){
  const sp=shortPath(p), i=sp.lastIndexOf('/');
  if(i<0||i===sp.length-1) return `<b>${esc(sp)}</b>`;
  return `${esc(sp.slice(0,i+1))}<b>${esc(sp.slice(i+1))}</b>`;
}
function shq(p){return /^[\w@%+=:,./~-]+$/.test(p)?p:"'"+p.replace(/'/g,"'\\''")+"'";}
function fmtDate(s){
  if(!s) return '';
  const d=new Date(s); if(isNaN(d)) return s;
  return d.toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function rel(ts){
  if(!ts) return '';
  const s=(Date.now()-ts)/1000;
  if(s<60) return 'just now';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  if(s<86400*7) return Math.floor(s/86400)+'d ago';
  const d=new Date(ts), sameYear=d.getFullYear()===new Date().getFullYear();
  return d.toLocaleDateString([],sameYear?{month:'short',day:'numeric'}:{year:'numeric',month:'short'});
}
function bucket(ts){
  if(!ts) return 'Unknown date';
  const t=new Date(); t.setHours(0,0,0,0); const day=t.getTime(), D=86400000;
  if(ts>=day) return 'Today';
  if(ts>=day-D) return 'Yesterday';
  if(ts>=day-6*D) return 'Previous 7 days';
  if(ts>=day-29*D) return 'Previous 30 days';
  return new Date(ts).toLocaleDateString([],{year:'numeric',month:'long'});
}
function titleOf(s){return oneLine(s&&s.summary)||'';}
function find(id){return state.sessions.find(s=>s.id===id);}
function toast(msg,kind){
  const el=$('toast'); el.textContent=msg; el.className='toast show'+(kind?' '+kind:'');
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.className='toast',1800);
}
async function copy(text,label){
  try{await navigator.clipboard.writeText(text);}
  catch(e){
    const ta=document.createElement('textarea'); ta.value=text; document.body.appendChild(ta);
    ta.select(); try{document.execCommand('copy');}catch(_){} ta.remove();
  }
  toast((label||'Copied')+' to clipboard');
}
function openFolder(path){
  if(!path) return;
  fetch('/api/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})
    .then(r=>{if(!r.ok) toast('Folder no longer exists','error');})
    .catch(()=>toast('Could not open folder','error'));
}

/* ---------- data ---------- */
async function load(){
  try{
    const r=await fetch('/api/sessions');
    state.sessions=await r.json();
  }catch(e){toast('Failed to load sessions','error');return;}
  for(const s of state.sessions){
    s._ts=Date.parse(s.modified)||0;
    s._loc=s.folder_path||s.project||'';
    s._hay=[s.id,s.project,s.summary,s.folder_path,s.source].join(' ').toLowerCase();
  }
  const ids=new Set(state.sessions.map(s=>s.id));
  for(const id of [...state.selected]) if(!ids.has(id)) state.selected.delete(id);
  if(state.activeId&&!ids.has(state.activeId)) closeDetail();
  render();
}
async function rescan(){
  const btn=$('refreshBtn'); btn.classList.add('spin');
  try{await fetch('/api/refresh',{method:'POST'}); await load();}
  finally{btn.classList.remove('spin');}
}

/* ---------- filtering ---------- */
function cutoff(){
  if(state.time==='all') return 0;
  const n=parseInt(state.time), u=state.time.slice(-1);
  return Date.now()-n*(u==='h'?3600000:86400000);
}
function matches(s,skip){
  if(skip!=='source'&&state.source!=='all'&&s.source!==state.source) return false;
  if(state.lost&&s.readable!==false) return false;
  if(state.maxMsgs>0&&(+s.message_count||0)>state.maxMsgs) return false;
  const c=cutoff(); if(c&&s._ts&&s._ts<c) return false;
  if(skip!=='project'&&state.project!==null&&s._loc!==state.project) return false;
  if(state.query&&!s._hay.includes(state.query)) return false;
  return true;
}
const SORTS={
  'modified-desc':(a,b)=>b._ts-a._ts,
  'modified-asc':(a,b)=>a._ts-b._ts,
  'msgs-desc':(a,b)=>(+b.message_count||0)-(+a.message_count||0)||b._ts-a._ts,
  'project-asc':(a,b)=>a._loc.localeCompare(b._loc)||b._ts-a._ts
};

/* ---------- render ---------- */
function render(){renderSidebar();renderList();renderBar();}

function renderSidebar(){
  const base=state.sessions.filter(s=>matches(s,'source'));
  const c={all:base.length,claude:0,codex:0};
  for(const s of base) if(s.source in c) c[s.source]++;
  document.querySelectorAll('[data-source]').forEach(b=>{
    b.classList.toggle('on',b.dataset.source===state.source);
    b.querySelector('.n').textContent=c[b.dataset.source]||0;
  });
  document.querySelectorAll('[data-time]').forEach(b=>b.classList.toggle('on',b.dataset.time===state.time));
  const lost=state.sessions.filter(s=>s.readable===false).length;
  $('lostCount').textContent=lost||'';
  $('lostToggle').checked=state.lost;

  const pm=new Map();
  for(const s of state.sessions) if(s._loc&&matches(s,'project')) pm.set(s._loc,(pm.get(s._loc)||0)+1);
  const all=[...pm].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));
  const LIMIT=8;
  let shown=state.allProjects?all:all.slice(0,LIMIT);
  if(state.project!==null&&!shown.some(([p])=>p===state.project)) shown=[[state.project,pm.get(state.project)||0],...shown];
  $('projects').innerHTML=shown.length?shown.map(([p,n])=>{
    const sp=shortPath(p), name=sp.split('/').filter(Boolean).pop()||sp;
    return `<button class="nav proj${p===state.project?' on':''}" data-project="${esc(p)}" title="${esc(sp)}"><span class="label">${esc(name)}</span><span class="n">${n}</span></button>`;
  }).join(''):'<div class="empty-mini">No directories</div>';
  const t=$('projToggle');
  t.hidden=all.length<=LIMIT;
  t.textContent=state.allProjects?'Show less':`Show all ${all.length}`;
}

function rowHtml(s){
  const sel=state.selected.has(s.id), act=s.id===state.activeId, lost=s.readable===false;
  const title=titleOf(s);
  const cls='row'+(act?' active':'')+(sel?' selected':'')+(lost?' lost':'');
  return `<div class="${cls}" data-sid="${esc(s.id)}" role="option" aria-selected="${act}">
    <label class="check" title="Select (x)"><input type="checkbox" data-id="${esc(s.id)}"${sel?' checked':''}></label>
    <div class="row-main">
      <div class="row-title">${title?esc(title):'<span class="muted">No prompt</span>'}</div>
      <div class="row-meta">
        <span class="src ${esc(s.source)}">${esc(SRC[s.source]||s.source)}</span>
        ${lost?'<span class="badge danger">File missing</span>':''}
        ${s._loc?`<span class="loc" title="${esc(s._loc)}">${pathHtml(s._loc)}</span>`:''}
      </div>
    </div>
    <div class="row-side">
      <time title="${esc(fmtDate(s.modified))}">${esc(rel(s._ts))}</time>
      <span>${s.message_count?esc(s.message_count)+' msgs':''}</span>
    </div>
    ${s.folder_path?`<button class="icon-btn row-open" data-folder="${esc(s.folder_path)}" title="Open ${esc(s.folder_path)}">${ICON.folder}</button>`:'<span class="row-gap"></span>'}
  </div>`;
}

function renderList(){
  const list=state.sessions.filter(s=>matches(s)).sort(SORTS[state.sort]||SORTS['modified-desc']);
  visible=list;
  const el=$('list');
  if(!list.length){
    const filtered=hasFilters();
    el.innerHTML=`<div class="list-empty"><div class="t">${state.sessions.length?'No matching sessions':'No sessions yet'}</div>
      <div>${filtered?'Try a different search or clear filters.':'Sessions from ~/.claude and ~/.codex will appear here.'}</div>
      ${filtered?'<button class="btn" data-clear-filters>Clear filters</button>':''}</div>`;
    return;
  }
  const groupFn=state.sort.startsWith('modified')?s=>bucket(s._ts)
    :state.sort==='project-asc'?s=>shortPath(s._loc)||'No directory':null;
  let html='';
  if(groupFn){
    const counts=new Map(); for(const s of list){const g=groupFn(s);counts.set(g,(counts.get(g)||0)+1);}
    let last=null;
    for(const s of list){
      const g=groupFn(s);
      if(g!==last){html+=`<div class="group">${esc(g)}<span class="gn">${counts.get(g)}</span></div>`;last=g;}
      html+=rowHtml(s);
    }
  }else{
    html=list.map(rowHtml).join('');
  }
  el.innerHTML=html;
}

function hasFilters(){
  return state.source!=='all'||state.time!=='all'||state.lost||state.maxMsgs>0||state.project!==null||!!state.query;
}
function renderBar(){
  const n=state.selected.size;
  document.body.classList.toggle('selecting',n>0);
  $('bulk').hidden=n===0;
  $('selCount').textContent=n;
  const sa=$('selectAll');
  const nSel=visible.filter(s=>state.selected.has(s.id)).length;
  sa.checked=visible.length>0&&nSel===visible.length;
  sa.indeterminate=nSel>0&&nSel<visible.length;
  const total=state.sessions.length;
  $('countLabel').innerHTML=visible.length===total?`<b>${total}</b> sessions`:`<b>${visible.length}</b> of ${total}`;

  const chips=[];
  if(state.source!=='all') chips.push(['source',SRC[state.source]]);
  if(state.time!=='all') chips.push(['time','Last '+document.querySelector(`[data-time="${state.time}"]`).textContent]);
  if(state.project!==null) chips.push(['project',shortPath(state.project).split('/').pop()||state.project]);
  if(state.lost) chips.push(['lost','Missing files']);
  if(state.maxMsgs>0) chips.push(['maxMsgs','≤ '+state.maxMsgs+' msgs']);
  $('fchips').innerHTML=chips.map(([k,l])=>`<span class="fchip"><span>${esc(l)}</span><button data-unset="${k}" title="Remove filter">${ICON.x}</button></span>`).join('')
    +(chips.length>1?'<button class="linkish" data-clear-filters>Clear all</button>':'');
}

function setActiveRow(noScroll){
  document.querySelectorAll('.row.active').forEach(r=>{r.classList.remove('active');r.setAttribute('aria-selected','false');});
  if(!state.activeId) return;
  const r=document.querySelector(`.row[data-sid="${CSS.escape(state.activeId)}"]`);
  if(r){r.classList.add('active');r.setAttribute('aria-selected','true');if(!noScroll)r.scrollIntoView({block:'nearest'});}
}

/* ---------- detail ---------- */
function emptyDetail(){
  $('detail').innerHTML=`<div class="d-empty">${ICON.chat}
    <div class="t">No session selected</div><div>Pick a session to read its transcript.</div>
    <dl class="keys"><dt><kbd>↑</kbd> <kbd>↓</kbd></dt><dd>Move</dd><dt><kbd>/</kbd></dt><dd>Search</dd>
    <dt><kbd>x</kbd></dt><dd>Select</dd><dt><kbd>c</kbd></dt><dd>Copy resume command</dd><dt><kbd>esc</kbd></dt><dd>Close</dd></dl></div>`;
}
function closeDetail(){
  state.activeId=null; state.detail=null; detailToken++;
  document.body.classList.remove('detail-open');
  setActiveRow(); emptyDetail();
}
function selectSession(sid,delay){
  state.activeId=sid; setActiveRow();
  document.body.classList.add('detail-open');
  clearTimeout(detailTimer);
  const s=find(sid);
  $('detail').innerHTML=headHtml(s,null)+'<div class="transcript"><div class="sk" style="width:40%"></div><div class="sk"></div><div class="sk" style="width:80%"></div></div>';
  detailTimer=setTimeout(()=>loadDetail(sid),delay||0);
}
async function loadDetail(sid){
  const tok=++detailToken;
  let d;
  try{const r=await fetch('/api/messages?id='+encodeURIComponent(sid)); d=await r.json();}
  catch(e){d={error:'Request failed'};}
  if(tok!==detailToken) return;
  state.detail=d.error?null:d; state.role='all';
  if(d.error){
    $('detail').innerHTML=`<div class="d-scroll">${headHtml(find(sid),null)}<div class="d-note err">${esc(d.error)}</div></div>`;
    return;
  }
  renderDetail();
}
function headHtml(s,d){
  if(!s&&!d) return '';
  const src=(d||s).source, id=(d||s).id;
  const dir=(d&&d.folder_path)||(s&&s.folder_path)||'';
  const project=(d&&d.project)||(s&&s.project)||'';
  const title=titleOf(s)||(d&&d.messages.find(m=>m.role==='user')?oneLine(d.messages.find(m=>m.role==='user').text).slice(0,160):'')||'Untitled session';
  const modified=(d&&d.modified)||(s&&s.modified)||'';
  const count=d?(d.message_count||d.messages.length):(s&&s.message_count)||'';
  const lost=s&&s.readable===false;
  const showProject=project&&project!==dir&&!(src==='claude'&&dir);
  let html=`<div class="d-top">
      <button class="icon-btn only-mobile" data-act="close" title="Back">${ICON.back}</button>
      <span class="pill ${esc(src)}">${esc(SRC[src]||src)}</span>
      ${lost?'<span class="badge danger">File missing</span>':''}
      <div class="spacer"></div>
      ${dir?`<button class="icon-btn" data-act="open" title="Open folder">${ICON.folder}</button>`:''}
      <button class="icon-btn" data-act="close" title="Close (esc)">${ICON.x}</button>
    </div>
    <div class="d-head">
      <h2 class="d-title" title="${esc(title)}">${esc(title)}</h2>
      <dl class="d-meta">
        ${showProject?`<dt>Project</dt><dd>${esc(project)}</dd>`:''}
        ${dir?`<dt>Directory</dt><dd class="mono" title="${esc(dir)}">${esc(shortPath(dir))}</dd>`:''}
        <dt>Session</dt><dd><span class="mono">${esc(id)}</span><button class="mini" data-copy="${esc(id)}" data-label="Session ID">Copy</button></dd>
        <dt>Updated</dt><dd>${esc(fmtDate(modified))}${modified?` <span class="muted">· ${esc(rel(Date.parse(modified)))}</span>`:''}</dd>
        ${count?`<dt>Messages</dt><dd>${esc(count)}</dd>`:''}
      </dl>`;
  if(d&&d.resume_cmd){
    const withCd=dir?'cd '+shq(dir)+' && '+d.resume_cmd:'';
    html+=`<div class="cmd">
        <div class="cmd-line"><span class="p">$</span><code>${esc(d.resume_cmd)}</code></div>
        <div class="cmd-actions">
          <button class="btn sm" data-copy="${esc(d.resume_cmd)}" data-label="Resume command">${ICON.copy}Copy</button>
          ${withCd?`<button class="btn sm ghost" data-copy="${esc(withCd)}" data-label="Command">Copy with cd</button>`:''}
        </div>
      </div>`;
  }
  return html+'</div>';
}
function msgHtml(m,i){
  const long=m.text.length>1400;
  return `<article class="msg ${m.role==='user'?'user':'assistant'}${long?' collapsed':''}">
    <header><span class="who">${m.role==='user'?'You':'Assistant'}</span><span>#${i+1}</span>
      <button class="mini" data-copy-msg="${i}">Copy</button></header>
    <div class="body">${esc(m.text)}</div>
    ${long?'<button class="more" data-more>Show more</button>':''}
  </article>`;
}
function renderDetail(){
  const d=state.detail; if(!d) return;
  const s=find(d.id);
  const nUser=d.messages.filter(m=>m.role==='user').length;
  let body='';
  d.messages.forEach((m,i)=>{if(state.role==='all'||m.role==='user') body+=msgHtml(m,i);});
  if(!body) body='<div class="d-note">No messages</div>';
  const prev=$('detail').querySelector('.d-scroll');
  const keepScroll=prev&&prev.dataset.sid===d.id?prev.scrollTop:0;
  $('detail').innerHTML=`<div class="d-scroll" data-sid="${esc(d.id)}">${headHtml(s,d)}
    <div class="d-tabs"><span class="label">Transcript</span>
      <div class="seg">
        <button data-role="all" class="${state.role==='all'?'on':''}">All<span class="c">${d.messages.length}</span></button>
        <button data-role="user" class="${state.role==='user'?'on':''}">Prompts<span class="c">${nUser}</span></button>
      </div>
    </div>
    <div class="transcript">${body}</div></div>`;
  if(keepScroll) $('detail').querySelector('.d-scroll').scrollTop=keepScroll;
}

/* ---------- actions ---------- */
function toggleSelect(id,on){
  if(on===undefined) on=!state.selected.has(id);
  on?state.selected.add(id):state.selected.delete(id);
  const r=document.querySelector(`.row[data-sid="${CSS.escape(id)}"]`);
  if(r){r.classList.toggle('selected',on);const cb=r.querySelector('input');if(cb)cb.checked=on;}
  renderBar();
}
function move(step){
  if(!visible.length) return;
  let i=visible.findIndex(s=>s.id===state.activeId);
  i=i<0?(step>0?0:visible.length-1):Math.max(0,Math.min(visible.length-1,i+step));
  selectSession(visible[i].id,140);
}
function askDelete(){
  const ids=[...state.selected]; if(!ids.length) return;
  $('dlgTitle').textContent=`Delete ${ids.length} session${ids.length>1?'s':''}?`;
  const items=ids.slice(0,30).map(id=>{const s=find(id);return `<li>${esc(titleOf(s)||id)}</li>`;}).join('');
  $('dlgList').innerHTML=items+(ids.length>30?`<li class="muted">and ${ids.length-30} more…</li>`:'');
  const dlg=$('confirmDlg');
  dlg.returnValue='';
  dlg.showModal();
  $('dlgOk').focus();
  dlg.onclose=async()=>{
    if(dlg.returnValue!=='ok') return;
    const btn=$('deleteBtn'); btn.disabled=true;
    try{
      const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
      const res=await r.json();
      toast(`Deleted ${res.deleted} session${res.deleted===1?'':'s'}`);
    }catch(e){toast('Delete failed','error');}
    btn.disabled=false;
    state.selected.clear();
    if(ids.includes(state.activeId)) closeDetail();
    await load();
  };
}
function unset(k){
  if(k==='source') state.source='all';
  else if(k==='time') state.time='all';
  else if(k==='project') state.project=null;
  else if(k==='lost') state.lost=false;
  else if(k==='maxMsgs'){state.maxMsgs=0;$('maxMsgs').value='';}
}
function clearFilters(){
  ['source','time','project','lost','maxMsgs'].forEach(unset);
  state.query=''; $('search').value='';
  render();
}

/* ---------- theme ---------- */
const THEMES=['system','light','dark'];
function applyTheme(t){
  if(t==='system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme=t;
  $('themeBtn').innerHTML=ICON[t==='light'?'sun':t==='dark'?'moon':'system'];
  $('themeBtn').title='Theme: '+t;
}
let theme=store.get('theme','system'); if(!THEMES.includes(theme)) theme='system';
applyTheme(theme);
$('themeBtn').addEventListener('click',()=>{
  theme=THEMES[(THEMES.indexOf(theme)+1)%THEMES.length];
  store.set('theme',theme); applyTheme(theme);
});

/* ---------- events ---------- */
$('list').addEventListener('click',e=>{
  if(e.target.closest('[data-clear-filters]')){clearFilters();return;}
  const cb=e.target.closest('input[type=checkbox]');
  if(cb){toggleSelect(cb.dataset.id,cb.checked);return;}
  if(e.target.closest('.check')) return;
  const ob=e.target.closest('.row-open');
  if(ob){openFolder(ob.dataset.folder);return;}
  const row=e.target.closest('.row[data-sid]');
  if(!row) return;
  if(e.metaKey||e.ctrlKey||e.shiftKey){toggleSelect(row.dataset.sid);return;}
  selectSession(row.dataset.sid);
});
$('detail').addEventListener('click',e=>{
  const t=e.target.closest('button'); if(!t) return;
  if(t.dataset.act==='close') closeDetail();
  else if(t.dataset.act==='open') openFolder((state.detail&&state.detail.folder_path)||(find(state.activeId)||{}).folder_path);
  else if(t.dataset.copy!==undefined) copy(t.dataset.copy,t.dataset.label);
  else if(t.dataset.copyMsg!==undefined&&state.detail) copy(state.detail.messages[+t.dataset.copyMsg].text,'Message');
  else if(t.dataset.role){state.role=t.dataset.role;renderDetail();}
  else if(t.dataset.more!==undefined){
    const m=t.closest('.msg'), c=m.classList.toggle('collapsed');
    t.textContent=c?'Show more':'Show less';
  }
});
$('side').addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  if(b.dataset.source){state.source=b.dataset.source;render();}
  else if(b.dataset.time){state.time=b.dataset.time;render();}
  else if(b.dataset.project!==undefined){state.project=state.project===b.dataset.project?null:b.dataset.project;render();}
  else if(b.id==='projToggle'){state.allProjects=!state.allProjects;renderSidebar();}
});
document.querySelector('.list-bar').addEventListener('click',e=>{
  const u=e.target.closest('[data-unset]');
  if(u){unset(u.dataset.unset);render();return;}
  if(e.target.closest('[data-clear-filters]')) clearFilters();
});
$('lostToggle').addEventListener('change',e=>{state.lost=e.target.checked;render();});
$('maxMsgs').addEventListener('input',e=>{state.maxMsgs=Math.max(0,parseInt(e.target.value)||0);render();});
let searchTimer=null;
$('search').addEventListener('input',e=>{
  clearTimeout(searchTimer);
  searchTimer=setTimeout(()=>{state.query=e.target.value.trim().toLowerCase();render();},80);
});
$('sort').value=SORTS[state.sort]?state.sort:'modified-desc';
$('sort').addEventListener('change',e=>{state.sort=e.target.value;store.set('sort',state.sort);renderList();setActiveRow(true);});
$('selectAll').addEventListener('change',e=>{
  for(const s of visible) e.target.checked?state.selected.add(s.id):state.selected.delete(s.id);
  render();
});
$('clearSel').addEventListener('click',()=>{state.selected.clear();render();});
$('deleteBtn').addEventListener('click',askDelete);
$('refreshBtn').addEventListener('click',rescan);
$('autoRefresh').addEventListener('change',e=>{
  clearInterval(autoTimer); autoTimer=null;
  if(e.target.checked) autoTimer=setInterval(rescan,30000);
  store.set('auto',e.target.checked?'1':'0');
});
if(store.get('auto','0')==='1'){$('autoRefresh').checked=true;autoTimer=setInterval(rescan,30000);}
$('menuBtn').addEventListener('click',()=>document.body.classList.add('side-open'));
$('scrim').addEventListener('click',()=>document.body.classList.remove('side-open'));

document.addEventListener('keydown',e=>{
  if($('confirmDlg').open) return;
  const t=e.target, search=$('search');
  const typing=t.matches&&t.matches('input:not([type=checkbox]),textarea,select');
  if(e.key==='/'&&!typing){e.preventDefault();search.focus();search.select();return;}
  if(e.key==='Escape'){
    if(t===search){
      if(search.value){search.value='';state.query='';render();} else search.blur();
      return;
    }
    if(document.body.classList.contains('side-open')){document.body.classList.remove('side-open');return;}
    if(state.activeId){closeDetail();return;}
    if(state.selected.size){state.selected.clear();render();}
    return;
  }
  if(t===search&&e.key==='ArrowDown'){e.preventDefault();search.blur();move(1);return;}
  if(typing||e.metaKey||e.ctrlKey||e.altKey) return;
  if(e.key==='ArrowDown'||e.key==='j'){e.preventDefault();move(1);}
  else if(e.key==='ArrowUp'||e.key==='k'){e.preventDefault();move(-1);}
  else if(e.key==='x'&&state.activeId){toggleSelect(state.activeId);}
  else if(e.key==='c'&&state.detail&&state.detail.resume_cmd){copy(state.detail.resume_cmd,'Resume command');}
  else if(e.key==='o'&&state.activeId){openFolder((find(state.activeId)||{}).folder_path);}
});

/* ---------- resizable detail ---------- */
(function(){
  const resizer=$('resizer'), panel=$('detail');
  const saved=parseInt(store.get('detailWidth','0'));
  if(saved>=320) panel.style.width=Math.min(saved,window.innerWidth-560)+'px';
  let startX, startW;
  resizer.addEventListener('mousedown',e=>{
    startX=e.clientX; startW=panel.offsetWidth;
    resizer.classList.add('active'); document.body.style.cursor='col-resize';
    document.addEventListener('mousemove',onMove);
    document.addEventListener('mouseup',onUp);
    e.preventDefault();
  });
  function onMove(e){
    const w=Math.max(320,Math.min(window.innerWidth-580,startW+startX-e.clientX));
    panel.style.width=w+'px';
  }
  function onUp(){
    resizer.classList.remove('active'); document.body.style.cursor='';
    store.set('detailWidth',panel.offsetWidth);
    document.removeEventListener('mousemove',onMove);
    document.removeEventListener('mouseup',onUp);
  }
})();

emptyDetail();
load();
setInterval(()=>{if(!document.hidden){renderList();setActiveRow(true);}},60000);
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            page = HTML_PAGE.replace("PLACEHOLDER_SCRIPT", JS_CODE)
            self._respond(200, page, "text/html")
        elif self.path == "/api/sessions":
            self._respond(200, json.dumps(_sessions_cache), "application/json")
        elif self.path.startswith("/api/messages?"):
            qs = parse_qs(urlparse(self.path).query)
            sid = qs.get("id", [""])[0]
            data = load_messages(sid)
            self._respond(200, json.dumps(data, ensure_ascii=False), "application/json")
        else:
            self._respond(404, "Not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/delete":
            body = self._read_body()
            ids = body.get("ids", [])
            n = delete_sessions(ids)
            self._respond(200, json.dumps({"deleted": n}), "application/json")
        elif self.path == "/api/refresh":
            scan_all()
            self._respond(200, json.dumps({"count": len(_sessions_cache)}), "application/json")
        elif self.path == "/api/open":
            body = self._read_body()
            folder = body.get("path", "")
            if folder and os.path.isdir(folder):
                self._respond(200, json.dumps({"ok": _open_path(folder)}), "application/json")
            else:
                self._respond(400, json.dumps({"error": "Invalid path"}), "application/json")
        else:
            self._respond(404, "Not found", "text/plain")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _respond(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # suppress request logs


def main():
    args = _parse_args(sys.argv[1:])
    if args.command == "list":
        return _command_list(args)
    if args.command in {"resume", "start"}:
        return _command_resume(args)
    if args.command == "pick":
        return _command_pick(args)
    return _command_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
