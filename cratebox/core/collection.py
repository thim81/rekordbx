"""Serialized client for the generic direct-access Rekordbox collection MCP."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path

from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.client.transports import StdioTransport

logger = logging.getLogger(__name__)


class CollectionConnectionError(Exception):
    status_code = 503

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class CollectionClient:
    """Own connection lifecycle and serialize calls to the collection provider."""

    def __init__(self, project: Path):
        self.state = "idle"
        self.message = None
        self.client = None
        self.lock = asyncio.Lock()
        self.call_lock = asyncio.Lock()
        self.project = Path(project)
        self.track_paths: dict[str, Path] = {}

    async def status(self):
        async with self.call_lock:
            if self.client and not self.client.is_connected():
                await self._invalidate(self.client)
            return {"state": self.state, "message": self.message}

    async def connect(self):
        async with self.lock:
            if self.client:
                return self.client
            self.state = "starting"
            self.message = None
            args = ['run', '--locked', '--offline', 'rekordbx-mcp', 'start']
            if os.environ.get('REKORDBOX_DATABASE'):
                args += ['--source', os.environ['REKORDBOX_DATABASE']]
            if os.environ.get('REKORDBOX_BACKUP_DIRECTORY'):
                args += ['--backup-directory', os.environ['REKORDBOX_BACKUP_DIRECTORY']]
            transport = StdioTransport(
                command="uv",
                args=args,
                cwd=str(self.project),
            )
            client = Client(transport, timeout=120, init_timeout=120)
            try:
                await client.__aenter__()
                tool_names = {tool.name for tool in await client.list_tools()}
                required_tools = {
                    "get_collection_tracks", "get_track_details",
                    "get_playlists", "get_playlist_tracks",
                }
                if not required_tools <= tool_names:
                    raise RuntimeError("A required Rekordbox collection tool is unavailable.")
                self.client = client
                self.state = "connected"
                return client
            except Exception as error:
                logger.exception("Failed to start the Rekordbox collection MCP")
                with suppress(Exception):
                    await client.close()
                self.state = "error"
                self.message = "Could not connect to the Rekordbox collection. Close Rekordbox and its background agents, then try again."
                raise CollectionConnectionError(self.message) from error

    async def call_tool(self, name: str, arguments: dict | None = None):
        async with self.call_lock:
            attempts = 2 if name in {
                "get_track_details", "get_collection_tracks", "get_playlists",
                "get_playlist_tracks", "get_playlist_detail", "get_track_file_path",
            } else 1
            for attempt in range(attempts):
                client = await self.connect()
                try:
                    return await client.call_tool(name, arguments or {})
                except ToolError:
                    # A rejected operation is a valid tool response, not a lost transport.
                    raise
                except Exception:
                    logger.exception("Rekordbox MCP tool %s failed; dropping the worker session", name)
                    await self._invalidate(client)
                    if attempt == attempts - 1:
                        raise

    async def _invalidate(self, client):
        with suppress(Exception):
            await client.close()
        if self.client is client:
            self.client = None
        self.state = "error"
        self.message = "Collection connection was lost. Try Lookup again or restart the local API."

    async def close(self):
        async with self.call_lock:
            async with self.lock:
                if self.client:
                    with suppress(Exception):
                        await self.client.close()
                    self.client = None
                self.track_paths.clear()
                self.state = "idle"
                self.message = None
