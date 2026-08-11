"""tg-cli — Telegram CLI entry point."""

import json
import logging
import os
import sys

import click

from ..console import console
from ..db import MessageDB
from .data import data_group
from .query import query_group
from .tg import tg_group

HOOK_TEMPLATE_CLAUDE = """
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "tg --toon 2>/dev/null || echo 'tg-cli not in PATH'"
          }
        ]
      }
    ]
  }
}
"""

HOOK_TEMPLATE_CODEX = """
{
  "hooks": {
    "SessionStart": [
      {
        "command": "tg --toon 2>/dev/null || echo 'tg-cli not in PATH'"
      }
    ]
  }
}
"""

HOOK_TEMPLATE_OPENCODE = """
# tg-cli OpenCode plugin
import subprocess
import json

async def on_start():
    try:
        result = subprocess.run(['tg', '--toon'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}

async def on_chat_end():
    return {}
"""


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _get_bin_path() -> str:
    """Get the absolute path of the current executable."""
    return os.path.abspath(sys.argv[0])


def _get_bin_path_with_tilde() -> str:
    """Get executable path with ~ for home directory."""
    path = _get_bin_path()
    home = os.path.expanduser("~")
    if path.startswith(home):
        return path.replace(home, "~", 1)
    return path


def _show_home_view():
    """Display the content-first home view with live data (AXI §8)."""
    # Try to get live data from local DB
    stats = {"total_messages": 0, "chat_count": 0, "chats": []}

    # Get local DB stats
    try:
        with MessageDB() as db:
            chats = db.get_chats()
            stats["total_messages"] = db.count()
            stats["chat_count"] = len(chats)
            stats["chats"] = chats[:5]  # Top 5 for preview
    except Exception:
        pass

    # Print bin path and description (AXI §10)
    bin_path = _get_bin_path_with_tilde()
    console.print(f"bin: {bin_path}")
    console.print(
        "description: Telegram CLI for syncing chats, searching messages, and local analysis"
    )
    console.print("")

    # Auth status - based on local DB only
    if stats["chat_count"] > 0:
        console.print("auth: [green]✓[/green] Previously authenticated (local data exists)")
    else:
        console.print("auth: [red]✗[/red] Not authenticated (run [bold]tg status[/bold] to check)")

    console.print("")

    # Live data preview
    if stats["chat_count"] > 0:
        console.print(f"chats: {stats['chat_count']} total ({stats['total_messages']} messages)")
        console.print("")

        # Show recent chats
        if stats["chats"]:
            console.print("recent:")
            for c in stats["chats"]:
                console.print(f"  {c['chat_id']}  {c['chat_name'] or '—'}  {c['msg_count']} msgs")
            if stats["chat_count"] > 5:
                console.print(f"  ... and {stats['chat_count'] - 5} more")
            console.print("")

        console.print(f"help: Run [bold]tg chats[/bold] to list all {stats['chat_count']} chats")
        console.print("help: Run [bold]tg sync-all[/bold] to refresh from Telegram")
        console.print("help: Run [bold]tg search <keyword>[/bold] to search messages")
    else:
        console.print("chats: 0 chats found in local database")
        console.print("")
        console.print("help: Run [bold]tg refresh[/bold] to sync from Telegram")
        console.print("help: Run [bold]tg status[/bold] to check authentication")

    console.print("")
    console.print("[dim]Run 'tg --help' for all commands[/dim]")


@click.group(invoke_without_command=True)
@click.version_option(package_name="kabi-tg-cli")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool):
    """tg — Telegram CLI for syncing chats, searching messages, and local analysis."""
    _setup_logging(verbose)

    # Content-first: if no subcommand invoked, show home view with live data
    if ctx.invoked_subcommand is None:
        _show_home_view()
        ctx.exit(0)


# Register ALL commands at top-level (flat structure, no `tg tg` nonsense)
for group in (tg_group, query_group, data_group):
    for name, cmd in group.commands.items():
        cli.add_command(cmd, name)


@cli.command()
@click.option("--claude", is_flag=True, help="Install Claude Code hook (~/.claude/settings.json)")
@click.option("--codex", is_flag=True, help="Install Codex hook (~/.codex/hooks.json)")
@click.option(
    "--opencode", is_flag=True, help="Install OpenCode plugin (~/.config/opencode/plugins/tg-cli/)"
)
@click.option("--all", "all_hooks", is_flag=True, help="Install all supported hooks")
def setup(claude: bool, codex: bool, opencode: bool, all_hooks: bool):
    """Install session hooks for AI agents (AXI §7).

    Examples:
        tg setup --all              # Install all hooks
        tg setup --claude           # Install Claude Code hook
        tg setup --codex --opencode # Install Codex and OpenCode hooks
    """
    import os
    import shutil
    from pathlib import Path

    bin_path = shutil.which("tg") or _get_bin_path()
    if not os.path.exists(bin_path):
        console.print(f"[red]tg binary not found at {bin_path}[/red]")
        return

    if all_hooks:
        claude = codex = opencode = True

    if not any([claude, codex, opencode]):
        console.print(
            "[yellow]No target specified. Use --claude, --codex, --opencode, or --all[/yellow]"
        )
        return

    # --- Claude Code hook ---
    if claude:
        hook_path = Path.home() / ".claude" / "settings.json"
        hook_path.parent.mkdir(parents=True, exist_ok=True)

        if hook_path.exists():
            with open(hook_path) as f:
                settings = json.load(f)
        else:
            settings = {}

        if "hooks" not in settings:
            settings["hooks"] = {}
        if "SessionStart" not in settings["hooks"]:
            settings["hooks"]["SessionStart"] = []

        # Normalize to list of dicts with hooks
        if settings["hooks"]["SessionStart"] and not isinstance(
            settings["hooks"]["SessionStart"][0], dict
        ):
            # Old format: list of strings
            settings["hooks"]["SessionStart"] = [
                {"hooks": [{"type": "command", "command": c}]}
                for c in settings["hooks"]["SessionStart"]
            ]

        # Check if our hook already exists
        hook_cmd = f"{bin_path} --toon 2>/dev/null || echo 'tg-cli not in PATH'"
        existing = False
        for hook in settings["hooks"]["SessionStart"]:
            if isinstance(hook, dict) and "hooks" in hook:
                for h in hook["hooks"]:
                    if h.get("command", "").startswith(bin_path):
                        existing = True
                        break

        if not existing:
            settings["hooks"]["SessionStart"].append(
                {"matcher": "*", "hooks": [{"type": "command", "command": hook_cmd}]}
            )
            with open(hook_path, "w") as f:
                json.dump(settings, f, indent=2)
            console.print(f"[green]✓[/green] Claude Code hook installed: {hook_path}")
        else:
            console.print("[dim]Claude Code hook already exists[/dim]")

    # --- Codex hook ---
    if codex:
        hook_path = Path.home() / ".codex" / "hooks.json"
        hook_path.parent.mkdir(parents=True, exist_ok=True)

        if hook_path.exists():
            with open(hook_path) as f:
                hooks = json.load(f)
        else:
            hooks = {"hooks": {"SessionStart": []}}

        hook_cmd = f"{bin_path} --toon 2>/dev/null || echo 'tg-cli not in PATH'"
        existing = False
        for hook in hooks.get("hooks", {}).get("SessionStart", []):
            if hook.get("command", "").startswith(bin_path):
                existing = True
                break

        if not existing:
            hooks.setdefault("hooks", {}).setdefault("SessionStart", []).append(
                {"command": hook_cmd}
            )
            with open(hook_path, "w") as f:
                json.dump(hooks, f, indent=2)
            console.print(f"[green]✓[/green] Codex hook installed: {hook_path}")
        else:
            console.print("[dim]Codex hook already exists[/dim]")

    # --- OpenCode plugin ---
    if opencode:
        plugin_dir = Path.home() / ".config" / "opencode" / "plugins" / "tg-cli"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        plugin_file = plugin_dir / "index.py"

        plugin_content = f"""# tg-cli OpenCode plugin
import subprocess
import json

async def on_start():
    try:
        result = subprocess.run(['{bin_path}', '--toon'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {{}}

async def on_chat_end():
    return {{}}
"""
        plugin_file.write_text(plugin_content)
        console.print(f"[green]✓[/green] OpenCode plugin installed: {plugin_file}")

    console.print("\n[dim]Restart your agent session to activate hooks.[/dim]")


if __name__ == "__main__":
    cli()
