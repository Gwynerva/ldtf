"""MCP server of LDTF: AI agents search and read the archives (read-only).

  protocol.py  JSON-RPC dispatcher for both eras of MCP: 2026-07-28 (stateless: server/discover, `_meta` on every
               request) and the initialize handshake of 2025-11-25 / 2025-06-18 / 2025-03-26
  tools.py     the tools: list_archives, search, count, get_post, get_comment, list_posts, list_comments,
               get_history, archive_stats (view.sqlite of each archive, opened per call)
  stdio.py     `python -m dtf_backup mcp`: newline-delimited JSON-RPC on stdin/stdout, works without the app
The running app also serves the same tools over Streamable HTTP at POST /mcp (web/server.py), with a Bearer token.
"""
