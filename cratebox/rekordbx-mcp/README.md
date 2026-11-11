# Rekordbox MCP adapter

This consumer-neutral adapter wraps the pinned [thim81/rekordbox-mcp](https://github.com/thim81/rekordbox-mcp) and pyrekordbox. It reads the original SQLCipher database and permits playlist creation, renaming, membership changes, swaps, and ordering. CrateBox-specific behavior stays in its API and UI.

## Startup

Close Rekordbox and its background agents, then run:

```sh
uv --directory cratebox/rekordbx-mcp sync --locked --python 3.12
uv --directory cratebox/rekordbx-mcp run --locked rekordbx-mcp start
```

The default source is `~/Library/Pioneer/rekordbox/master.db`. Override it with `--source /absolute/path/master.db`; use `--backup-directory /absolute/path/backups` to choose backup storage outside the original database directory. CrateBox's API passes `REKORDBOX_DATABASE` and `REKORDBOX_BACKUP_DIRECTORY` through to those options when set.

Startup and every collection operation require Rekordbox and its agents to be closed. Unreadable process inventories, pending rollback journals, and unrecognized transaction sidecars reject access. Normal WAL/SHM files are supported; SQLite handles their committed state. Connections close after each request and reconnect on the next request. The adapter accepts a database file. Verified backups support recovery through the commands below.

## Tools

The reviewed upstream reading-tool allowlist remains explicit. `get_collection_tracks` resolves ordered IDs, exact absolute locations, or pages of active collection records. The upstream `get_track_file_path` resolves the location of one active track by ID. These location tools are an internal contract for trusted local application clients. Keep them out of conversational MCP allowlists. Other collection responses redact file and database paths.

`get_playlist_state` returns the playlist ID, name, editable flag, revision, and ordered entries containing `entry_id`, `track_id`, and one-based `position`. `get_playlist_tracks` includes `playlist_entry_id`. The internal `get_playlist_detail` tool returns state, tracks, and file locations from one read transaction. Keep it out of conversational MCP allowlists. Playlists with unresolved collection references remain viewable but cannot be edited.

`edit_playlist` accepts `create`, `rename`, `add`, `remove`, `swap`, or `move`. Existing-playlist operations require the revision returned by `get_playlist_state`. Remove, swap and move use playlist-entry IDs, not collection-track IDs. `move_playlist_track(playlist_id, playlist_song_id, position, expected_revision)` wraps pyrekordbox's existing reorder method. Multi-entry moves preserve playlist order within the moved group.

Only the transaction owner commits: library-internal removal commits flush instead. Compound edits commit once, preserve unaffected entry IDs and relative order, and roll back before-commit failures. Process checks run again before committing. Database lock errors are returned without mutation retries. The permitted tool registry excludes playlist deletion, imports, cleanup and arbitrary reconnect operations. Upstream stays pinned and unmodified; updates must remain explicit through `update-upstream` with compatibility tests and rollback.

## Backups and restoration

The adapter follows upstream's 300-second cooldown policy but replaces its warning-only copy behavior. Before the first edit in a session, it creates a recovery point outside the original database directory. It uses SQLCipher's consistent SQLite backup API, including committed data still in the WAL, then verifies hashes and encrypted integrity. Backup verification opens the completed backup immutably, so it does not create new journal sidecars. Rollback journals still block access. Required companion XML is included. Backup failure blocks writing. A verified recovery point can be reused within the cooldown while unchanged; external database changes invalidate the interval. No timer runs while idle, and backups are not automatically deleted.

Default storage: repository `.cratebox/rekordbox-backups/`. Recovery points include a manifest, `master.db`, and `masterPlaylists6.xml` when present. Normal user permissions apply; this is not OS isolation.

```sh
uv --directory cratebox/rekordbx-mcp run --locked rekordbx-mcp backup-list
uv --directory cratebox/rekordbx-mcp run --locked rekordbx-mcp restore --backup-id BACKUP_ID
```

Use matching `--source` and `--backup-directory` options for non-default installations. Restore validates the recovery point, requires a successful SQLite WAL checkpoint and closed clients when WAL files are present, preserves current files first (even damaged/missing files), then replaces and validates the database and companion. Do not restore while any collection client is active. An interrupted or uncertain commit/restore leaves a recovery-required marker and blocks further mutations. SQLite and XML cannot commit atomically together; explicit recovery may undo edits made since the selected backup. No automatic restore or retry occurs.

## Verification

```sh
uv --directory cratebox/rekordbx-mcp run --locked pytest -q
```

Tests use synthetic encrypted databases for actual create/rename/add/remove/reorder/swap operations, duplicate-entry identity, revision conflicts, rollback, process checks, backup cooldown, corruption/missing-file recovery, companion updates/failures, forbidden tools, and a direct MCP stdio session. Never run development mutation tests against the original collection.
