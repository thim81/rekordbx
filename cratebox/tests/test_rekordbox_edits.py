import asyncio
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import rekordbox_playlists as api


def test_playlist_edit_forwards_entry_ids_and_revision_without_retry(monkeypatch):
    calls = []
    async def tool(name, arguments=None):
        calls.append((name, arguments))
        return {'id':'100','name':'Set','revision':'new','editable':True,'entries':[]}
    monkeypatch.setattr(api, 'call_tool', tool)
    assert hasattr(api, 'PlaylistEdit')
    result = asyncio.run(api.edit_rekordbox_playlist('100', api.PlaylistEdit(
        action='move', expected_revision='old', entry_ids=['503'], position=1,
    ), api.Request({"type":"http", "headers":[(b"origin", b"http://localhost:4444")]})))
    assert result['revision'] == 'new'
    assert calls == [('edit_playlist', dict(action='move', playlist_id='100', expected_revision='old',
        name=None, entry_ids=['503'], track_ids=None, position=1, target_entry_id=None, before=True))]


def test_detail_uses_one_collection_read_and_keeps_its_revision(monkeypatch):
    async def tool(name, arguments=None):
        assert (name, arguments) == ('get_playlist_detail', {'playlist_id': '100'})
        return {'id': '100', 'revision': 'r', 'editable': True, 'tracks': []}
    monkeypatch.setattr(api, 'call_tool', tool)
    assert asyncio.run(api.get_rekordbox_playlist_detail('100')) == {
        'id': '100', 'name': '', 'revision': 'r', 'editable': True, 'tracks': [],
    }


def test_database_mutations_reject_remote_browser_origins():
    request = api.Request({'type':'http', 'headers':[(b'origin', b'https://untrusted.example')]})
    with pytest.raises(api.HTTPException) as caught:
        api.require_local_playlist_origin(request)
    assert caught.value.status_code == 403


def test_playlist_summary_keeps_total_duration(monkeypatch):
    async def records():
        return [{'id':'100', 'name':'Set', 'track_count':3, 'duration':640}]
    monkeypatch.setattr(api, 'playlist_records', records)
    summaries = asyncio.run(api.get_rekordbox_playlists())
    assert summaries[0]['duration'] == 640


def test_tool_error_keeps_the_specific_safe_api_error(monkeypatch):
    from fastmcp.exceptions import ToolError
    async def fail(*_args, **_kwargs):
        raise ToolError('Database transaction sidecar exists; close Rekordbox cleanly.')
    monkeypatch.setattr(api.collection_mcp, 'call_tool', fail)
    with pytest.raises(api.HTTPException) as caught:
        asyncio.run(api.call_tool('edit_playlist', {'action':'move'}))
    assert caught.value.status_code == 409
    assert 'transaction files' in caught.value.detail
