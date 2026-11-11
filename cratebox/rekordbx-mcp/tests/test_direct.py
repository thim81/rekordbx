import pytest

from rekordbx_mcp.backup import encrypted_connection


@pytest.fixture
def library(encrypted_database, tmp_path, monkeypatch):
    monkeypatch.setattr('rekordbx_mcp.access.psutil.process_iter', lambda *_: [])
    source = tmp_path / 'original' / 'master.db'
    source.parent.mkdir()
    encrypted_database.rename(source)
    from sqlcipher3 import dbapi2 as sqlite
    from pyrekordbox.masterdb.database import BLOB
    from pyrekordbox.utils import deobfuscate
    with sqlite.connect(source) as conn:
        conn.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
        conn.execute("INSERT INTO agentRegistry (registry_id, int_1) VALUES ('localUpdateCount', 0)")
        conn.execute("INSERT INTO djmdContent (ID, Title, rb_local_deleted) VALUES ('2', 'Second', 0)")
        conn.execute("INSERT INTO djmdPlaylist (ID, Name, Attribute, ParentID, Seq, rb_local_deleted) VALUES ('100', 'Set', 0, 'root', 1, 0)")
        conn.executemany("INSERT INTO djmdSongPlaylist (ID, PlaylistID, ContentID, TrackNo, rb_local_deleted) VALUES (?, '100', ?, ?, 0)", [('501','1',1),('502','2',2),('503','1',3)])
    return source


def service(library, tmp_path):
    from rekordbx_mcp import direct
    assert hasattr(direct, 'PlaylistEditor')
    return direct.PlaylistEditor(library, tmp_path / 'backups')


def test_move_uses_entry_id_preserves_duplicates_and_revision(library, tmp_path):
    editor = service(library, tmp_path)
    state = editor.read('100')
    result = editor.edit('move', '100', state['revision'], entry_ids=['503'], position=1)
    assert [e['entry_id'] for e in result['entries']] == ['503', '501', '502']
    assert [e['track_id'] for e in result['entries']] == ['1', '1', '2']
    assert result['revision'] != state['revision']
    with pytest.raises(ValueError, match='changed'):
        editor.edit('remove', '100', state['revision'], entry_ids=['501'])
    assert len(editor.backups.list()) == 1
    restored = editor.edit('move', '100', result['revision'], entry_ids=['503'], position=3)
    assert restored == state



def test_swap_is_atomic_even_after_library_internal_commit(library, tmp_path, monkeypatch):
    from pyrekordbox import MasterDatabase
    editor = service(library, tmp_path)
    state = editor.read('100')
    original = MasterDatabase.add_to_playlist
    def fail(*args, **kwargs):
        raise RuntimeError('insertion failed')
    monkeypatch.setattr(MasterDatabase, 'add_to_playlist', fail)
    with pytest.raises(RuntimeError, match='insertion failed'):
        editor.edit('swap', '100', state['revision'], entry_ids=['503'], track_ids=['2'])
    assert editor.read('100') == state
    monkeypatch.setattr(MasterDatabase, 'add_to_playlist', original)
    result = editor.edit('swap', '100', state['revision'], entry_ids=['503'], track_ids=['2'])
    assert [e['track_id'] for e in result['entries']] == ['1', '2', '2']
    assert [e['entry_id'] for e in result['entries'][:2]] == ['501', '502']


def test_create_rename_add_remove_and_preserve_track_metadata(library, tmp_path):
    editor = service(library, tmp_path)
    new = editor.edit('create', name='New set')
    pid = new['id']
    state = editor.edit('rename', pid, new['revision'], name='Renamed')
    state = editor.edit('add', pid, state['revision'], track_ids=['2', '1', '2'])
    assert [e['track_id'] for e in state['entries']] == ['2','1','2']
    kept = state['entries'][1]['entry_id']
    state = editor.edit('remove', pid, state['revision'], entry_ids=[state['entries'][0]['entry_id'], state['entries'][2]['entry_id']])
    assert state['entries'] == [{'entry_id': kept, 'track_id': '1', 'position': 1}]
    assert state['id'] == pid and state['name'] == 'Renamed'
    with encrypted_connection(library) as conn:
        assert conn.execute("SELECT Title, Rating FROM djmdContent WHERE ID='1'").fetchone() == ('One More Time', 4)


async def test_each_read_checks_processes_when_opening_sqlite(library, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from rekordbx_mcp.server import build_server

    checks = []
    running = []
    def processes(*_):
        checks.append(True)
        return running

    monkeypatch.setattr('rekordbx_mcp.access.psutil.process_iter', processes)
    async with Client(build_server(library, tmp_path / 'backups')) as client:
        await client.call_tool('get_track_file_path', {'track_id': '1'})
        assert len(checks) == 1
        running.append(SimpleNamespace(info={'name': 'rekordboxAgent'}))
        library.with_name('masterPlaylists6.xml').write_text('<unused/>')
        def unexpected_access(*_args, **_kwargs):
            raise AssertionError('must check Rekordbox before inspecting sidecars or companion XML')
        monkeypatch.setattr('rekordbx_mcp.direct.reject_rollback_journal', unexpected_access)
        monkeypatch.setattr('rekordbx_mcp.direct.MasterPlaylistXml', unexpected_access)
        with pytest.raises(ToolError, match='Close Rekordbox'):
            await client.call_tool('get_track_file_path', {'track_id': '1'})
        assert len(checks) == 2


async def test_mcp_reads_entry_ids_and_moves_without_exposing_paths(library, tmp_path):
    import json
    from fastmcp import Client
    from rekordbx_mcp.server import build_server
    async with Client(build_server(library, tmp_path / 'backups')) as client:
        state = (await client.call_tool('get_playlist_state', {'playlist_id':'100'})).data
        if 'result' in state:
            state = state['result']
        tracks = await client.call_tool('get_playlist_tracks', {'playlist_id':'100'})
        tracks = json.loads(tracks.content[0].text)
        assert [t['playlist_entry_id'] for t in tracks] == ['501','502','503']
        assert all('file_path' not in t for t in tracks)
        detail = (await client.call_tool('get_playlist_detail', {'playlist_id': '100'})).structured_content
        assert detail['revision'] == state['revision']
        assert [t['playlist_entry_id'] for t in detail['tracks']] == ['501', '502', '503']
        assert detail['tracks'][0]['file_path'] == '/private/original/music/track.mp3'
        moved = await client.call_tool('move_playlist_track', dict(playlist_id='100', playlist_song_id='503', position=1, expected_revision=state['revision']))
        assert not moved.is_error
        names = {t.name for t in await client.list_tools()}
        assert not names & {'delete_playlist','import_track','remove_broken_tracks','connect_database'}


def test_reads_reject_transaction_sidecars_without_recovering(library, tmp_path):
    editor = service(library, tmp_path)
    before = library.read_bytes()
    library.with_name('master.db-journal').write_bytes(b'pending recovery')
    with pytest.raises(ValueError, match='sidecar'):
        editor.read('100')
    assert library.read_bytes() == before


async def test_orphaned_entries_remain_browsable_but_not_editable(library, tmp_path):
    import json
    from fastmcp import Client
    from rekordbx_mcp.server import build_server
    from sqlcipher3 import dbapi2 as sqlite
    from pyrekordbox.masterdb.database import BLOB
    from pyrekordbox.utils import deobfuscate
    with sqlite.connect(library) as conn:
        conn.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
        conn.execute("UPDATE djmdSongPlaylist SET ContentID='999' WHERE ID='502'")
    async with Client(build_server(library, tmp_path / 'backups')) as client:
        result = await client.call_tool('get_playlist_tracks', {'playlist_id':'100'})
        assert [t['playlist_entry_id'] for t in json.loads(result.content[0].text)] == ['501','503']
        summaries = (await client.call_tool('get_playlists')).structured_content['result']
        assert [(row['track_count'], row['duration']) for row in summaries] == [(3, 640)]
        state = service(library, tmp_path).read('100')
        assert state['editable'] is False
        assert [e['entry_id'] for e in state['entries']] == ['501','503']


async def test_detail_reads_revision_and_metadata_from_the_same_transaction(library, tmp_path, monkeypatch):
    from contextlib import closing
    from fastmcp import Client
    from pyrekordbox.masterdb.database import BLOB
    from pyrekordbox.utils import deobfuscate
    from sqlcipher3 import dbapi2 as sqlite
    from rekordbx_mcp import server

    with closing(sqlite.connect(library)) as writer:
        writer.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        original_state = server.playlist_state

        def change_after_state(db, playlist_id):
            state = original_state(db, playlist_id)
            with closing(sqlite.connect(library)) as other:
                other.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
                other.execute("UPDATE djmdContent SET Title='New title' WHERE ID='1'")
                other.commit()
            return state

        monkeypatch.setattr(server, 'playlist_state', change_after_state)
        async with Client(server.build_server(library, tmp_path / 'backups')) as client:
            detail = (await client.call_tool('get_playlist_detail', {'playlist_id': '100'})).structured_content
            assert detail['tracks'][0]['title'] == 'One More Time'
        assert writer.execute("SELECT Title FROM djmdContent WHERE ID='1'").fetchone()[0] == 'New title'


@pytest.fixture
def companion(library):
    from datetime import datetime
    from sqlcipher3 import dbapi2 as sqlite
    from pyrekordbox.masterdb.database import BLOB
    from pyrekordbox.utils import deobfuscate
    now = datetime.now()
    with sqlite.connect(library) as conn:
        conn.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
        conn.execute('UPDATE djmdPlaylist SET updated_at=?', (now.isoformat(' '),))
    path = library.with_name('masterPlaylists6.xml')
    path.write_text(f'<MASTER_PLAYLIST Version="1" AutomaticSync="0"><PRODUCT Version="7"/><PLAYLISTS><NODE Id="64" ParentId="0" Attribute="0" Timestamp="{int(now.timestamp()*1000)}" Lib_Type="0" CheckType="0"/></PLAYLISTS></MASTER_PLAYLIST>')
    return path


def test_creation_updates_companion_and_restore_recovers_both(library, tmp_path, companion):
    from pyrekordbox.masterdb.aux_files import MasterPlaylistXml
    editor = service(library, tmp_path)
    original = companion.read_bytes()
    created = editor.edit('create', name='Created with XML')
    assert MasterPlaylistXml(path=companion).get(created['id']) is not None
    point = editor.backups.list()[0]['id']
    editor.backups.restore(point)
    assert companion.read_bytes() == original
    with pytest.raises(ValueError, match='not found'):
        editor.read(created['id'])


def test_companion_failure_blocks_further_writes_until_recovery(library, tmp_path, companion, monkeypatch):
    from pyrekordbox.masterdb.aux_files import MasterPlaylistXml
    editor = service(library, tmp_path)
    def fail(_self):
        raise OSError('companion failed')
    monkeypatch.setattr(MasterPlaylistXml, 'save', fail)
    with pytest.raises(RuntimeError, match='commit outcome is uncertain'):
        editor.edit('create', name='Uncertain')
    assert editor.backups.blocked.exists()
    with pytest.raises(RuntimeError, match='recovery required'):
        editor.edit('create', name='Must block')
    editor.backups.restore(editor.backups.list()[0]['id'])
    assert not editor.backups.blocked.exists()


def test_agents_starting_during_flush_are_checked_at_session_commit(library, tmp_path, monkeypatch):
    from pyrekordbox.masterdb.registry import RekordboxAgentRegistry
    from rekordbx_mcp import direct
    editor = service(library, tmp_path)
    state = editor.read('100')
    started = [False]
    original = RekordboxAgentRegistry.autoincrement_local_update_count
    def update(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        started[0] = True
        return result
    def closed():
        if started[0]:
            raise RuntimeError('Close Rekordbox agent')
    monkeypatch.setattr(RekordboxAgentRegistry, 'autoincrement_local_update_count', update)
    monkeypatch.setattr(direct, 'require_rekordbox_closed', closed)
    with pytest.raises(RuntimeError, match='Close Rekordbox agent'):
        editor.edit('move', '100', state['revision'], entry_ids=['503'], position=1)
    assert not editor.backups.blocked.exists()
    started[0] = False
    assert editor.read('100') == state


def test_soft_deleted_entries_do_not_count_toward_active_positions(library, tmp_path):
    from sqlcipher3 import dbapi2 as sqlite
    from pyrekordbox.masterdb.database import BLOB
    from pyrekordbox.utils import deobfuscate
    with sqlite.connect(library) as conn:
        conn.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
        conn.execute("INSERT INTO djmdSongPlaylist (ID, PlaylistID, ContentID, TrackNo, rb_local_deleted) VALUES ('old', '100', '2', 4, 1)")
    editor = service(library, tmp_path)
    state = editor.read('100')
    state = editor.edit('add', '100', state['revision'], track_ids=['2'])
    assert [e['position'] for e in state['entries']] == [1,2,3,4]
    state = editor.edit('move', '100', state['revision'], entry_ids=[state['entries'][-1]['entry_id']], position=1)
    assert [e['position'] for e in state['entries']] == [1,2,3,4]
    with encrypted_connection(library) as conn:
        assert conn.execute("SELECT TrackNo, rb_local_deleted FROM djmdSongPlaylist WHERE ID='old'").fetchone() == (4,1)


async def test_playlist_summary_duration_counts_repeated_active_tracks(library, tmp_path):
    import json
    from fastmcp import Client
    from rekordbx_mcp.server import build_server
    async with Client(build_server(library, tmp_path / 'backups')) as client:
        result = await client.call_tool('get_playlists')
        playlists = json.loads(result.content[0].text)
        assert playlists[0]['duration'] == 640  # 320-second track appears twice; missing lengths count as zero


def test_wal_playlist_edit_and_recovery(library, tmp_path):
    from sqlcipher3 import dbapi2 as sqlite
    from pyrekordbox.masterdb.database import BLOB
    from pyrekordbox.utils import deobfuscate
    conn = sqlite.connect(library)
    conn.execute("PRAGMA key = '" + deobfuscate(BLOB) + "'")
    conn.execute('PRAGMA journal_mode=WAL')
    conn.close()
    editor = service(library, tmp_path)
    before = editor.read('100')
    after = editor.edit('move', '100', before['revision'], entry_ids=['503'], position=1)
    assert [e['entry_id'] for e in after['entries']] == ['503', '501', '502']
    assert len(editor.backups.list()) == 1
    editor.backups.restore(editor.backups.list()[0]['id'])
    assert editor.read('100') == before
