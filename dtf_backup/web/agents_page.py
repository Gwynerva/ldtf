""""Настройки приложения → ИИ-агенты" (/app/agents): how to connect an AI agent to the archives over MCP — a command
for Claude Code, a JSON block for other clients (stdio: the agent starts LDTF's own Python), the address and token of
the HTTP endpoint /mcp of the running app."""

from __future__ import annotations

import html
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config
from ..mcp.tools import TOOLS
from .icons import icon
from .ui import app_tabs, btn, page_head

if TYPE_CHECKING:
    from .server import App

E = html.escape
APP_ROOT = Path(__file__).resolve().parent.parent.parent
FLASH = "<!--flash-->"


def python_cmd() -> tuple[str, dict | None]:
    """(python.exe that runs `-m dtf_backup`, env it needs): the embedded runtime finds the app by itself; a system
    Python needs the app folder on PYTHONPATH. Never pythonw.exe: it has no stdin/stdout."""
    rt = APP_ROOT / "runtime" / "python.exe"
    if rt.exists():
        return str(rt), None
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return str(exe), {"PYTHONPATH": str(APP_ROOT)}


def code_box(cid: str, text: str, label: str) -> str:
    return (f'<div class="code-box"><div class="cb-h"><span>{E(label)}</span>'
            f'<button class="btn text sm" type="button" data-copy="{cid}">{icon("content_copy")}Копировать</button></div>'
            f'<pre><code id="{cid}">{E(text)}</code></pre></div>')


def app_agents_page(app: "App", base_url: str, notice: str = FLASH) -> str:
    docker = config.docker()
    default_root = (APP_ROOT / "archive").resolve()
    root_args = [] if app.library == default_root else ["--root", str(app.library)]
    if docker:
        cmd, args, env = "docker", ["exec", "-i", "ldtf", "python", "-m", "dtf_backup", "mcp"], None
    else:
        cmd, env = python_cmd()
        args = ["-X", "utf8", "-m", "dtf_backup", "mcp", *root_args]
    stdio_json = {"mcpServers": {"ldtf": {"command": cmd, "args": args, **({"env": env} if env else {})}}}
    claude = "claude mcp add --scope user" + "".join(f" -e {k}={v}" for k, v in (env or {}).items()) + \
        " ldtf -- " + subprocess.list2cmdline([cmd, *args])
    url = base_url.rstrip("/") + "/mcp"
    token = app.mcp_token()
    http_json = {"mcpServers": {"ldtf": {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}
    claude_http = f'claude mcp add --scope user --transport http ldtf {url} --header "Authorization: Bearer {token}"'
    tools = "".join(f'<li><code>{E(t["name"])}</code><span>{E(t["title"])}</span></li>' for t in TOOLS)
    intro = (f'<section class="card fgroup"><h2>{icon("smart_toy")}ИИ-агенты (MCP)</h2>'
             f'<p>LDTF подключается к ИИ-агентам — Claude, Cursor и другим клиентам MCP. Агент ищет по архивам и читает '
             f'посты, комментарии с контекстом, историю правок и статистику. Только чтение: ни архивы, ни DTF агент не '
             f'меняет.</p><ul class="mcp-tools">{tools}</ul></section>')
    where = ("Агент запускает LDTF внутри контейнера сам (контейнер должен называться ldtf)." if docker else
             "Агент сам запускает LDTF для чтения архивов; приложение при этом может быть и не запущено.")
    local = (f'<section class="card fgroup"><h2>{icon("terminal")}Агент на этом компьютере</h2><p>{where}</p>'
             + code_box("mcp-claude", claude, "Claude Code — выполните в терминале")
             + code_box("mcp-json", json.dumps(stdio_json, ensure_ascii=False, indent=2),
                        "Claude Desktop, Cursor и другие — блок для файла настроек MCP")
             + '<p class="muted small">Claude Desktop: Настройки → Developer → Edit Config, добавьте блок в '
               '<code>claude_desktop_config.json</code> и перезапустите Claude.</p>'
             + (f'<p class="muted small">В папке LDTF уже есть <code>.mcp.json</code>: Claude Code, запущенный в '
                f'<code>{E(str(APP_ROOT))}</code>, сам предложит подключить архивы.</p>'
                if not docker and not root_args and (APP_ROOT / ".mcp.json").exists() else "")
             + '</section>')
    reach = ("Работает, пока запущен LDTF." + ("" if app.remote else
             " LDTF сейчас доступен только с этого компьютера: для других устройств запустите его с --host или в Docker."))
    regen = app.shell.form("/app/agents/token", btn("Выпустить новый токен", "text sm", "key"),
                           confirm="Выпустить новый токен? Агенты со старым перестанут подключаться.")
    remote = (f'<section class="card fgroup"><h2>{icon("dns")}Агент по сети (HTTP)</h2>'
              f'<p>Адрес <code>{E(url)}</code>, доступ по токену. {E(reach)}</p>'
              + code_box("mcp-claude-http", claude_http, "Claude Code")
              + code_box("mcp-json-http", json.dumps(http_json, ensure_ascii=False, indent=2), "Другие клиенты")
              + f'<div class="mcp-token"><span class="muted small">Токен даёт чтение всех архивов. Если он попал не туда, '
                f'выпустите новый.</span>{regen}</div></section>')
    body = page_head("Настройки приложения") + app_tabs("agents") + notice + intro + local + remote
    return app.page("ИИ-агенты", body, active="agents")
