"""Reuse upstream reading tools behind an independent, deny-by-default registry."""

import argparse
import asyncio
import sys
from contextlib import asynccontextmanager, suppress
from functools import wraps
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from rekordbox_mcp import database as upstream_database
from rekordbox_mcp import server as upstream
from rekordbox_mcp.models import Playlist
from pyrekordbox.masterdb.models import DjmdContent, DjmdCue, DjmdPlaylist, DjmdSongPlaylist
from sqlalchemy import and_, func
from sqlalchemy.orm import joinedload

from .direct import DirectDatabase, PlaylistEditor, content_by_ids, playlist_state
from .backup import DEFAULT_BACKUP_DIRECTORY


READ_TOOLS = (
    "search_tracks", "get_track_details", "get_tracks_by_key",
    "get_tracks_by_bpm_range", "get_most_played_tracks", "get_top_rated_tracks",
    "get_unplayed_tracks", "search_tracks_by_filename", "analyze_library",
    "validate_track_ids",
    "get_library_stats", "get_history_sessions", "get_session_tracks",
    "get_recent_sessions", "get_history_stats", "search_history_sessions",
)
_upstream_lock = asyncio.Lock()
_database_thread_lock = Lock()


async def _serialized_database_to_thread(function, /, *args, **kwargs):
    def run():
        with _database_thread_lock:
            return function(*args, **kwargs)

    return await asyncio.to_thread(run)


# Upstream database methods use asyncio.to_thread internally, including close().
# Serialize work on the shared upstream session.
upstream_database.asyncio = SimpleNamespace(to_thread=_serialized_database_to_thread)
def redact_paths(value):
    if isinstance(value, dict):
        return {
            key: redact_paths(item) for key, item in value.items()
            if key not in ("file_path", "database_path")
        }
    if isinstance(value, list):
        return [redact_paths(item) for item in value]
    return value


def build_server(source: Path, backup_directory: Path = DEFAULT_BACKUP_DIRECTORY) -> FastMCP:
    if source.is_symlink() or not source.is_file():
        raise ValueError('Select an existing database file, not a symlink.')
    database = DirectDatabase(source)
    editor = PlaylistEditor(source, backup_directory)

    async def complete_before_cancellation(awaitable):
        task = asyncio.create_task(awaitable)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
            raise

    async def run_database_operation(function):
        return await complete_before_cancellation(_serialized_database_to_thread(function))

    async def ensure_connected():
        if not await database.is_connected():
            await database.connect()
            # Surface connection failures before upstream can treat them as missing tracks.
            await run_database_operation(database.db.session.connection)

    @asynccontextmanager
    async def session_scope():
        async with _upstream_lock:
            try:
                yield
            finally:
                if database.db is not None:
                    await complete_before_cancellation(database.disconnect())

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            async with _upstream_lock:
                await database.disconnect()

    server = FastMCP("Rekordbox collection", lifespan=lifespan)

    def wrap(original, tool_name: str):
        @wraps(original)
        async def call(*args, **kwargs):
            # Upstream's ORM session and module globals are shared. Serialize
            # requests, bind our configured database and replace every lazy reconnect path.
            async with session_scope():
                upstream.db = database
                upstream._db_initialized = True
                upstream.ensure_database_connected = ensure_connected
                await ensure_connected()
                result = await complete_before_cancellation(original(*args, **kwargs))
                return result if tool_name == "get_track_file_path" else redact_paths(result)
        return call

    for name in (*READ_TOOLS, "get_track_file_path"):
        upstream_tool = getattr(upstream, name)
        original = getattr(upstream_tool, "fn", upstream_tool)
        server.tool(
            name=name, annotations={"readOnlyHint": True, "destructiveHint": False},
        )(wrap(original, name))

    @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_playlists() -> list[dict]:
        """List active Rekordbox playlists and folders with counts and durations."""
        def read():
            session = database.db.session
            totals = session.query(
                DjmdSongPlaylist.PlaylistID.label("playlist_id"),
                func.count(DjmdSongPlaylist.ID).label("track_count"),
                func.sum(DjmdContent.Length).label("duration"),
            ).outerjoin(DjmdContent, and_(
                DjmdContent.ID == DjmdSongPlaylist.ContentID,
                DjmdContent.rb_local_deleted == 0,
            )).filter(DjmdSongPlaylist.rb_local_deleted == 0).group_by(DjmdSongPlaylist.PlaylistID).subquery()
            rows = session.query(DjmdPlaylist, totals.c.track_count, totals.c.duration).outerjoin(
                totals, totals.c.playlist_id == DjmdPlaylist.ID,
            ).filter(DjmdPlaylist.rb_local_deleted == 0).all()
            return [
                Playlist(
                    id=str(playlist.ID), name=playlist.Name or "", track_count=count or 0,
                    created_date=playlist.created_at or "", modified_date=playlist.updated_at or "",
                    is_folder=playlist.is_folder, is_smart_playlist=playlist.is_smart_playlist,
                    smart_criteria=str(playlist.SmartList) if playlist.is_smart_playlist and playlist.SmartList else None,
                    parent_id=str(playlist.ParentID) if playlist.ParentID and playlist.ParentID != "root" else None,
                ).model_dump() | {"duration": duration or 0, "sequence": int(playlist.Seq or 0)}
                for playlist, count, duration in rows
            ]

        async with session_scope():
            await ensure_connected()
            return await run_database_operation(read)

    def read_playlist_detail(playlist_id: str) -> dict:
        session = database.db.session
        # Use one SQLite read transaction for the revision, ordered entries and their metadata.
        session.connection().exec_driver_sql("BEGIN")
        state = playlist_state(database.db, playlist_id)
        ids = {entry["track_id"] for entry in state["entries"]}
        tracks = {}
        if ids:
            content = session.query(DjmdContent).options(
                joinedload(DjmdContent.Artist), joinedload(DjmdContent.Album),
                joinedload(DjmdContent.Genre), joinedload(DjmdContent.Key),
            ).filter(DjmdContent.ID.in_(ids)).all()
            cue_counts = dict(session.query(DjmdCue.ContentID, func.count(DjmdCue.ID)).filter(
                DjmdCue.ContentID.in_(ids), DjmdCue.rb_local_deleted == 0,
            ).group_by(DjmdCue.ContentID).all())
            memberships = {}
            for content_id, playlist_id, name in session.query(
                DjmdSongPlaylist.ContentID, DjmdPlaylist.ID, DjmdPlaylist.Name,
            ).join(DjmdPlaylist, DjmdPlaylist.ID == DjmdSongPlaylist.PlaylistID).filter(
                DjmdSongPlaylist.ContentID.in_(ids), DjmdSongPlaylist.rb_local_deleted == 0,
                DjmdPlaylist.rb_local_deleted == 0,
            ).distinct().order_by(DjmdPlaylist.Name):
                memberships.setdefault(str(content_id), []).append({"id": str(playlist_id), "name": name or ""})
            for row in content:
                key = str(row.ID)
                playlists = memberships.get(key, [])
                tracks[key] = database._content_to_track(row).model_dump() | {
                    "cue_count": cue_counts.get(key, 0),
                    "playlist_count": len(playlists),
                    "playlists": playlists,
                }
        return {**state, "tracks": [
            {**tracks[entry["track_id"]], "playlist_entry_id": entry["entry_id"]}
            for entry in state["entries"]
        ]}

    @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_playlist_detail(playlist_id: str) -> dict:
        """Read one playlist's revision and ordered tracks. File locations are internal to trusted local clients."""
        async with session_scope():
            await ensure_connected()
            return await run_database_operation(lambda: read_playlist_detail(playlist_id))

    @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_playlist_tracks(playlist_id: str) -> list[dict]:
        """Read ordered playlist tracks and entry IDs, omitting local file paths."""
        detail = await get_playlist_detail(playlist_id)
        return redact_paths(detail["tracks"])

    @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_collection_tracks(
        track_ids: list[str] | None = None,
        file_paths: list[str] | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[dict]:
        """Resolve ordered IDs or exact file locations, or page through active generic collection tracks. Internal consumer contract."""
        if (
            (track_ids is not None and file_paths is not None)
            or ((track_ids is not None or file_paths is not None) and offset != 0)
            or offset < 0 or not 1 <= limit <= 1000
        ):
            raise ToolError("Provide one selector or a valid page (offset >= 0, limit 1–1000).")
        if track_ids is not None and (not track_ids or len(track_ids) > 1000 or any(not isinstance(x, str) or not x for x in track_ids)):
            raise ToolError("Provide 1–1000 nonempty track IDs.")
        if file_paths is not None and (not file_paths or len(file_paths) > 1000 or any(
            not isinstance(x, str) or not Path(x).is_absolute() or any(c in x for c in "\r\n\x00\x85\u2028\u2029")
            for x in file_paths
        )):
            raise ToolError("Provide 1–1000 absolute file locations.")
        async with session_scope():
            upstream.db = database
            upstream._db_initialized = True
            upstream.ensure_database_connected = ensure_connected
            await ensure_connected()

            def resolve():
                query = database.db.get_content()
                model = query.column_descriptions[0]["entity"]
                query = query.filter(model.rb_local_deleted == 0)
                if track_ids is not None:
                    by_id = content_by_ids(database.db, track_ids)
                    if any(i not in by_id or by_id[i].rb_local_deleted for i in track_ids):
                        raise ValueError
                    rows = [by_id[i] for i in track_ids]
                elif file_paths is not None:
                    rows = []
                    for location in file_paths:
                        matches = query.filter(model.FolderPath == location).all()
                        if len(matches) != 1:
                            raise ValueError
                        rows.append(matches[0])
                else:
                    rows = query.order_by(model.ID).offset(offset).limit(limit).all()
                ids = list(dict.fromkeys(str(row.ID) for row in rows))
                cues = dict(database.db.session.query(DjmdCue.ContentID, func.count(DjmdCue.ID)).filter(
                    DjmdCue.ContentID.in_(ids), DjmdCue.rb_local_deleted == 0,
                ).group_by(DjmdCue.ContentID).all()) if ids else {}
                memberships = {}
                if ids:
                    for content_id, playlist_id, name in database.db.session.query(
                        DjmdSongPlaylist.ContentID, DjmdPlaylist.ID, DjmdPlaylist.Name,
                    ).join(DjmdPlaylist, DjmdPlaylist.ID == DjmdSongPlaylist.PlaylistID).filter(
                        DjmdSongPlaylist.ContentID.in_(ids), DjmdSongPlaylist.rb_local_deleted == 0,
                        DjmdPlaylist.rb_local_deleted == 0,
                    ).distinct().order_by(DjmdPlaylist.Name).all():
                        memberships.setdefault(str(content_id), []).append({"id": str(playlist_id), "name": str(name or "")})
                output = []
                for row in rows:
                    item = database._content_to_track(row).model_dump()
                    key = str(row.ID)
                    item.update(file_path=str(row.FolderPath or ""), bit_depth=int(row.BitDepth or 0),
                                sample_rate=int(row.SampleRate or 0), bitrate=int(row.BitRate or 0),
                                cue_count=cues.get(key, 0), playlists=memberships.get(key, []),
                                playlist_count=len(memberships.get(key, [])))
                    output.append(item)
                return output

            try:
                return await run_database_operation(resolve)
            except ValueError:
                raise ToolError("A selector must resolve to active collection tracks; each file location must match exactly one track.") from None

    @server.tool(annotations={"readOnlyHint": True})
    async def get_playlist_state(playlist_id: str) -> dict:
        """Current playlist entry identities, order and optimistic revision."""
        async with session_scope():
            await ensure_connected()
            return await run_database_operation(lambda: playlist_state(database.db, playlist_id))

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def edit_playlist(
        action: str, playlist_id: str = '', expected_revision: str = '',
        name: str | None = None, entry_ids: list[str] | None = None,
        track_ids: list[str] | None = None, position: int | None = None,
        target_entry_id: str | None = None, before: bool = True,
    ) -> dict:
        """Create, rename, add, remove, swap or move playlist entries as one persisted action."""
        async with _upstream_lock:
            try:
                return await run_database_operation(lambda: editor.edit(
                    action, playlist_id, expected_revision, name=name, entry_ids=entry_ids,
                    track_ids=track_ids, position=position, target_entry_id=target_entry_id, before=before,
                ))
            except (ValueError, RuntimeError, OSError) as error:
                raise ToolError(str(error)) from None

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def move_playlist_track(
        playlist_id: str, playlist_song_id: str, position: int, expected_revision: str,
    ) -> dict:
        """Move one playlist entry to a one-based position, preserving playlist and entry IDs."""
        return await edit_playlist('move', playlist_id, expected_revision,
                                   entry_ids=[playlist_song_id], position=position)

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, default=DEFAULT_BACKUP_DIRECTORY)
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    build_server(args.source, args.backup_directory).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
