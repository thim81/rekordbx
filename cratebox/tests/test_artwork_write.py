import copy
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest
from mutagen.id3 import APIC, ID3, POPM, TALB, TIT2, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "cratebox"))
from api import artwork_write, playlists


def png(color: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + color))
        + chunk(b"IEND", b"")
    )


def synthetic_mp3(path: Path) -> None:
    frame = bytes.fromhex("fffb9064") + bytes(413)
    path.write_bytes(frame * 20)
    audio = MP3(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text="Test track"))
    audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Front", data=png(b"\xff\x00\x00")))
    audio.tags.add(APIC(encoding=3, mime="image/png", type=4, desc="Back", data=png(b"\x00\x00\xff")))
    audio.save(v2_version=3)


def synthetic_m4a(path: Path) -> None:
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required to generate an M4A fixture")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1", "-c:a", "aac", str(path)],
        check=True,
    )


def mpeg_stream(path: Path) -> bytes:
    data = path.read_bytes()
    if data.startswith(b"ID3"):
        tag_size = sum((data[index] & 0x7F) << (7 * (3 - offset)) for offset, index in enumerate(range(6, 10)))
        return data[10 + tag_size :]
    return data


def id3_frames_except_cratebox_rating(tags):
    return sorted(
        (key, copy.deepcopy(frame.__dict__))
        for key, frame in tags.items()
        if not (isinstance(frame, POPM) and frame.email.casefold() == "cratebox")
    )


def playlist_fixture(tmp_path, monkeypatch, audio: Path, *, entry="42"):
    playlists_dir = tmp_path / "playlists"
    output_dir = tmp_path / "output"
    playlists_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    playlist = playlists_dir / "Set.m3u8"
    playlist.write_text(f"#EXTM3U\n{audio}\n", encoding="utf-8")
    (output_dir / "Set.json").write_text(
        json.dumps({"format": 1, "name": "Set", "tracks": [{"track_id": entry, "file_path": str(audio)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(playlists, "PLAYLIST_DIR", playlists_dir)
    monkeypatch.setattr(playlists, "DEFAULT_PLAYLIST_DIR", playlists_dir)
    monkeypatch.setattr(playlists, "CRATE_DIRECTORY", output_dir)
    return output_dir, playlist


def test_embed_cover_updates_original_and_preserves_mp3_tags_without_backup(tmp_path):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()
    original_stream = mpeg_stream(source)
    original_duration = MP3(source).info.length
    result = artwork_write.embed_cover_art(source, png(b"\x00\xff\x00"), "image/png")

    updated = MP3(source)
    assert result is None
    assert source.read_bytes() != original
    assert not (tmp_path / "audio-backups").exists()
    assert updated.info.length == pytest.approx(original_duration)
    assert mpeg_stream(source) == original_stream
    assert updated.tags.version == (2, 3, 0)
    assert str(updated.tags["TIT2"]) == "Test track"
    pictures = updated.tags.getall("APIC")
    assert [(frame.type, frame.desc) for frame in pictures] == [(4, "Back"), (3, "Front cover")]
    assert next(frame.data for frame in pictures if frame.type == 3) == png(b"\x00\xff\x00")


def test_resolve_playlist_mp3_uses_server_side_playlist_identity(tmp_path, monkeypatch):
    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    playlist_fixture(tmp_path, monkeypatch, source)

    assert artwork_write.resolve_playlist_mp3("Set", "42-1") == source
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.resolve_playlist_mp3("Set", str(source))


def test_resolve_playlist_mp3_rejects_symlink_and_hardlink(tmp_path, monkeypatch):
    target = tmp_path / "music" / "track.mp3"
    target.parent.mkdir()
    synthetic_mp3(target)
    link = tmp_path / "music" / "linked.mp3"
    link.symlink_to(target)
    output, playlist = playlist_fixture(tmp_path, monkeypatch, link)
    (output / "Set.json").write_text(
        json.dumps({"format": 1, "name": "Set", "tracks": [{"track_id": "42", "file_path": str(target)}]}),
        encoding="utf-8",
    )
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.resolve_playlist_mp3("Set", "42-1")

    hardlink = tmp_path / "music" / "hardlink.mp3"
    hardlink.hardlink_to(target)
    playlist.write_text(f"#EXTM3U\n{hardlink}\n", encoding="utf-8")
    (output / "Set.json").write_text(
        json.dumps({"format": 1, "name": "Set", "tracks": [{"track_id": "42", "file_path": str(hardlink)}]}),
        encoding="utf-8",
    )
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.resolve_playlist_mp3("Set", "42-1")


def test_invalid_image_leaves_mp3_unchanged(tmp_path):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()

    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.embed_cover_art(source, b"not an image", "image/png")

    assert source.read_bytes() == original


def test_set_audio_rating_updates_mp3_and_preserves_audio_and_tags(tmp_path):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    before = MP3(source)
    before.tags.add(TALB(encoding=3, text="Test album"))
    before.tags.add(TXXX(encoding=3, desc="Publisher", text="Example Records"))
    before.save(v2_version=3)
    original_tags = id3_frames_except_cratebox_rating(MP3(source).tags)
    original_stream = mpeg_stream(source)
    duration = MP3(source).info.length

    result = artwork_write.set_audio_rating(source, 4)

    updated = MP3(source)
    assert result is None
    assert next(frame.rating for frame in updated.tags.getall("POPM") if frame.email == "cratebox") == 204
    assert id3_frames_except_cratebox_rating(updated.tags) == original_tags
    assert mpeg_stream(source) == original_stream
    assert updated.info.length == pytest.approx(duration)
    assert not (tmp_path / "audio-backups").exists()


@pytest.mark.parametrize("operation", ["rating", "cover"])
@pytest.mark.parametrize(
    ("legacy_title", "has_modern_tags"),
    [(b"Test track", True), (b"Legacy title", True), (b"Legacy title", False)],
    ids=["matching-title", "conflicting-title", "legacy-only"],
)
def test_mp3_tag_edit_preserves_id3v1_without_copying_fields(tmp_path, operation, legacy_title, has_modern_tags):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    if not has_modern_tags:
        source.write_bytes(mpeg_stream(source))
    legacy_tag = (
        b"TAG"
        + legacy_title.ljust(30, b"\x00")
        + b"Legacy artist".ljust(30, b"\x00")
        + b"Legacy album".ljust(30, b"\x00")
        + b"1999"
        + b"Legacy comment".ljust(28, b"\x00")
        + b"\x00\x07"
        + b"\x11"
    )
    with source.open("ab") as audio_file:
        audio_file.write(legacy_tag)
    original_stream = mpeg_stream(source)

    cover = png(b"\x00\xff\x00")
    if operation == "rating":
        artwork_write.set_audio_rating(source, 4)
    else:
        artwork_write.embed_cover_art(source, cover, "image/png")

    tags = ID3(source, load_v1=False, v2_version=3)
    assert mpeg_stream(source) == original_stream
    assert source.read_bytes()[-128:] == legacy_tag
    assert str(tags.get("TIT2", "")) == ("Test track" if has_modern_tags else "")
    assert not {"TPE1", "TALB", "TYER", "TRCK", "TCON"}.intersection(tags)
    assert not tags.getall("COMM")
    if operation == "rating":
        assert tags["POPM:cratebox"].rating == 204
    else:
        assert [frame.data for frame in tags.getall("APIC") if frame.type == 3] == [cover]


def test_set_audio_rating_updates_m4a_and_preserves_other_tags(tmp_path):
    source = tmp_path / "track.m4a"
    synthetic_m4a(source)
    audio = MP4(source)
    audio.tags["\xa9nam"] = ["Test track"]
    audio.tags["\xa9ART"] = ["Test artist"]
    audio.tags["covr"] = [MP4Cover(png(b"\x00\xff\x00"), imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()
    original = MP4(source)
    duration = original.info.length
    original_tags = {
        key: copy.deepcopy(value)
        for key, value in original.tags.items()
        if key != "----:com.apple.iTunes:RATING"
    }

    artwork_write.set_audio_rating(source, 3)

    updated = MP4(source)
    assert updated.tags["----:com.apple.iTunes:RATING"] == [b"3"]
    assert {
        key: value
        for key, value in updated.tags.items()
        if key != "----:com.apple.iTunes:RATING"
    } == original_tags
    assert updated.info.length == pytest.approx(duration)


def test_set_audio_rating_zero_is_persisted_and_invalid_values_fail_safely(tmp_path):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    artwork_write.set_audio_rating(source, 0)
    assert any(frame.email == "cratebox" and frame.rating == 0 for frame in MP3(source).tags.getall("POPM"))
    original = source.read_bytes()

    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.set_audio_rating(source, 6)

    assert source.read_bytes() == original


def test_set_audio_rating_leaves_original_intact_when_replace_fails(tmp_path, monkeypatch):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()

    def fail_replace(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(artwork_write, "_replace_source", fail_replace)
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.set_audio_rating(source, 2)

    assert source.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_non_mp3_target_is_rejected_without_modification(tmp_path):
    source = tmp_path / "track.wav"
    source.write_bytes(b"RIFF not an mp3")
    original = source.read_bytes()

    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.embed_cover_art(source, png(b"\x01\x02\x03"), "image/png")

    assert source.read_bytes() == original


def test_download_cover_art_rejects_non_https_and_private_dns(monkeypatch):
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.download_cover_image("http://images.example/cover.png")

    monkeypatch.setattr(artwork_write.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (artwork_write.socket.AF_INET, artwork_write.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.download_cover_image("https://images.example/cover.png")


def test_download_cover_art_pins_public_address_and_returns_validated_png(monkeypatch):
    address = (artwork_write.socket.AF_INET, artwork_write.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(artwork_write.socket, "getaddrinfo", lambda *_args, **_kwargs: [address])
    requested = []

    def fetch(host, ip, target):
        requested.append((host, ip, target))
        return 200, {"content-type": "image/png"}, png(b"\x01\x02\x03")

    monkeypatch.setattr(artwork_write, "_fetch_https", fetch)

    data, mime = artwork_write.download_cover_image("https://images.example/covers/one.png?size=large")

    assert (data, mime) == (png(b"\x01\x02\x03"), "image/png")
    assert requested == [("images.example", "93.184.216.34", "/covers/one.png?size=large")]


def test_download_cover_art_follows_validated_cover_art_archive_redirect(monkeypatch):
    monkeypatch.setattr(
        artwork_write.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (artwork_write.socket.AF_INET, artwork_write.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    requested = []

    def fetch(host, _ip, target):
        requested.append((host, target))
        if host == "coverartarchive.org":
            return 307, {"location": "https://ia800.archive.org/download/cover/front.jpg"}, b""
        return 200, {"content-type": "image/png"}, png(b"\x01\x02\x03")

    monkeypatch.setattr(artwork_write, "_fetch_https", fetch)

    data, mime = artwork_write.download_cover_image(
        "https://coverartarchive.org/release/12345678-1234-4234-8234-123456789abc/front"
    )

    assert (data, mime) == (png(b"\x01\x02\x03"), "image/png")
    assert requested == [
        ("coverartarchive.org", "/release/12345678-1234-4234-8234-123456789abc/front"),
        ("ia800.archive.org", "/download/cover/front.jpg"),
    ]


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (302, "image/png", png(b"\x01\x02\x03")),
        (200, "text/html", png(b"\x01\x02\x03")),
        (200, "image/png", b"not png"),
        (200, "image/png", b"\x89PNG\r\n\x1a\n" + bytes(artwork_write.MAX_IMAGE_BYTES)),
    ],
)
def test_download_cover_art_rejects_redirects_bad_images_and_oversize(
    monkeypatch, status, content_type, body
):
    monkeypatch.setattr(artwork_write.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (artwork_write.socket.AF_INET, artwork_write.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr(
        artwork_write,
        "_fetch_https",
        lambda *_args: (status, {"content-type": content_type}, body),
    )

    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.download_cover_image("https://images.example/cover.png")


def test_artwork_update_does_not_create_a_backup_directory(tmp_path):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    artwork_write.embed_cover_art(source, png(b"\x01\x02\x03"), "image/png")
    assert not (tmp_path / "audio-backups").exists()


def test_tag_write_failure_does_not_change_source(tmp_path, monkeypatch):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()
    def fail_save(*_args, **_kwargs):
        raise OSError("tag write failed")

    monkeypatch.setattr(artwork_write.ID3, "save", fail_save)
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.embed_cover_art(source, png(b"\x01\x02\x03"), "image/png")

    assert source.read_bytes() == original


def test_replace_failure_does_not_change_source(tmp_path, monkeypatch):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()
    monkeypatch.setattr(artwork_write, "_replace_source", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.embed_cover_art(source, png(b"\x01\x02\x03"), "image/png")

    assert source.read_bytes() == original


def test_staged_validation_failure_does_not_change_source(tmp_path, monkeypatch):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()
    def fail_validation(*_args, **_kwargs):
        raise artwork_write.ArtworkWriteError("staged MP3 validation failed")

    monkeypatch.setattr(artwork_write, "_validate_staged", fail_validation)
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.embed_cover_art(source, png(b"\x01\x02\x03"), "image/png")

    assert source.read_bytes() == original


def test_concurrent_source_change_is_not_overwritten(tmp_path, monkeypatch):
    source = tmp_path / "track.mp3"
    synthetic_mp3(source)
    original = source.read_bytes()
    changed = original + b"changed concurrently"
    real_validate = getattr(artwork_write, "_validate_staged", lambda *_args: None)

    def change_source(*args, **kwargs):
        real_validate(*args, **kwargs)
        source.write_bytes(changed)

    monkeypatch.setattr(artwork_write, "_validate_staged", change_source)
    with pytest.raises(artwork_write.ArtworkWriteError):
        artwork_write.embed_cover_art(source, png(b"\x01\x02\x03"), "image/png")

    assert source.read_bytes() == changed


def test_embed_endpoint_writes_only_selected_mp3_and_clears_old_override(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from api import artwork_data

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    original = source.read_bytes()
    output, _ = playlist_fixture(tmp_path, monkeypatch, source)
    monkeypatch.setattr(artwork_data, "ARTWORK_FILE", output / "cratebox-artwork.json")
    (output / "cratebox-artwork.json").write_text(
        json.dumps({"playlists": {}, "tracks": {"Set": {"42-1": "https://old.example/cover.png"}}}),
        encoding="utf-8",
    )
    selected = png(b"\x00\xff\x00")
    monkeypatch.setattr(artwork_write, "download_cover_image", lambda _url: (selected, "image/png"))

    with TestClient(api_app) as client:
        response = client.put(
            "/api/artwork/embed",
            json={"playlist_id": "Set", "track_id": "42-1", "image_url": "https://images.example/cover.png"},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["backup_id"] is None
    assert "track.mp3" not in response.text
    assert "file_path" not in response.json()
    assert response.json()["artwork_url"].startswith("/api/playlists/Set/tracks/42-1/artwork?v=")
    assert source.read_bytes() != original
    assert next(frame.data for frame in MP3(source).tags.getall("APIC") if frame.type == 3) == selected
    assert artwork_data.read_artwork_overrides()["tracks"] == {}
    assert not (output / "audio-backups").exists()


def test_embed_endpoint_returns_a_fresh_artwork_url_for_each_update(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    playlist_fixture(tmp_path, monkeypatch, source)
    selected_images = [png(b"\x00\xff\x00"), png(b"\x00\x00\xff")]
    monkeypatch.setattr(
        artwork_write,
        "download_cover_image",
        lambda _url: (selected_images.pop(0), "image/png"),
    )
    request = {
        "playlist_id": "Set",
        "track_id": "42-1",
        "image_url": "https://images.example/cover.png",
    }

    with TestClient(api_app) as client:
        first = client.put("/api/artwork/embed", json=request, headers={"Origin": "http://localhost:4444"})
        second = client.put("/api/artwork/embed", json=request, headers={"Origin": "http://localhost:4444"})

    assert first.status_code == second.status_code == 200
    assert first.json()["artwork_url"] != second.json()["artwork_url"]


def test_embed_endpoint_rejects_untrusted_origin_without_downloading_or_writing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    original = source.read_bytes()
    playlist_fixture(tmp_path, monkeypatch, source)
    called = []
    monkeypatch.setattr(artwork_write, "download_cover_image", lambda _url: called.append(True))

    with TestClient(api_app) as client:
        response = client.put(
            "/api/artwork/embed",
            json={"playlist_id": "Set", "track_id": "42-1", "image_url": "https://images.example/cover.png"},
            headers={"Origin": "https://attacker.example"},
        )

    assert response.status_code == 403
    assert not called
    assert source.read_bytes() == original


def test_embed_endpoint_restores_old_override_when_mp3_write_fails(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from api import artwork_data

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    original = source.read_bytes()
    output, _ = playlist_fixture(tmp_path, monkeypatch, source)
    monkeypatch.setattr(artwork_data, "ARTWORK_FILE", output / "cratebox-artwork.json")
    old_override = "https://old.example/cover.png"
    (output / "cratebox-artwork.json").write_text(
        json.dumps({"playlists": {}, "tracks": {"Set": {"42-1": old_override}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(artwork_write, "download_cover_image", lambda _url: (png(b"\x01\x02\x03"), "image/png"))

    def fail_write(*_args, **_kwargs):
        raise artwork_write.ArtworkWriteError("staged write failed")

    monkeypatch.setattr(artwork_write, "embed_cover_art", fail_write)
    with TestClient(api_app) as client:
        response = client.put(
            "/api/artwork/embed",
            json={"playlist_id": "Set", "track_id": "42-1", "image_url": "https://images.example/cover.png"},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 400
    assert source.read_bytes() == original
    assert artwork_data.read_artwork_overrides()["tracks"]["Set"]["42-1"] == old_override


def test_rekordbox_embed_endpoint_resolves_track_id_and_updates_audio(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from api import rekordbox_playlists
    from api.collection import collection_mcp

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    monkeypatch.setattr(artwork_write, "download_cover_image", lambda _url: (png(b"\x01\x02\x03"), "image/png"))
    calls = []

    async def call_tool(name, arguments=None):
        calls.append((name, arguments))
        return {
            "get_track_file_path": {"track_id": "track-1", "file_path": str(source)},
        }[name]

    monkeypatch.setattr(rekordbox_playlists, "call_tool", call_tool)
    collection_mcp.track_paths.pop("track-1", None)
    with TestClient(api_app) as client:
        response = client.put(
            "/api/collection/tracks/track-1/artwork/embed",
            json={"image_url": "https://images.example/cover.png"},
            headers={"Origin": "http://localhost:4444"},
        )
    collection_mcp.track_paths.pop("track-1", None)

    assert response.status_code == 200, response.text
    assert "file_path" not in response.text
    assert response.json()["backup_id"] is None
    assert response.json()["artwork_url"].startswith("/api/collection/tracks/track-1/artwork?v=")
    assert calls == [("get_track_file_path", {"track_id": "track-1"})]
    assert next(frame.data for frame in MP3(source).tags.getall("APIC") if frame.type == 3) == png(b"\x01\x02\x03")
    assert not (tmp_path / "backups").exists()


def test_rekordbox_embed_endpoint_rejects_invalid_location(monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from api import rekordbox_playlists

    called = []

    async def call_tool(name, arguments=None):
        called.append(name)
        return {"get_track_file_path": {"track_id": "other-track", "file_path": "relative.mp3"}}[name]

    monkeypatch.setattr(rekordbox_playlists, "call_tool", call_tool)
    monkeypatch.setattr(artwork_write, "download_cover_image", lambda _url: pytest.fail("image should not download"))
    with TestClient(api_app) as client:
        response = client.put(
            "/api/collection/tracks/other-track/artwork/embed",
            json={"image_url": "https://images.example/cover.png"},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 409
    assert called == ["get_track_file_path"]


def test_crate_rating_endpoint_updates_only_the_audio_rating(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from core.audio_metadata import read_embedded_rating

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    playlist_fixture(tmp_path, monkeypatch, source)

    with TestClient(api_app) as client:
        response = client.put(
            "/api/playlists/Set/tracks/42-1/rating",
            json={"rating": 4},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"rating": 4}
    assert str(source) not in response.text and "file_path" not in response.text
    assert read_embedded_rating(source) == 4


def test_rekordbox_rating_endpoint_resolves_track_id_and_never_writes_collection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from api import rekordbox_playlists
    from core.audio_metadata import read_embedded_rating

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    calls = []

    async def call_tool(name, arguments=None):
        calls.append((name, arguments))
        return {
            "get_track_file_path": {"track_id": "track-1", "file_path": str(source)},
        }[name]

    monkeypatch.setattr(rekordbox_playlists, "call_tool", call_tool)
    with TestClient(api_app) as client:
        response = client.put(
            "/api/collection/tracks/track-1/rating",
            json={"rating": 5},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"rating": 5}
    assert str(source) not in response.text and "file_path" not in response.text
    assert calls == [("get_track_file_path", {"track_id": "track-1"})]
    assert read_embedded_rating(source) == 5


def test_crate_rating_endpoint_rejects_out_of_range_values_without_writing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    playlist_fixture(tmp_path, monkeypatch, source)
    original = source.read_bytes()

    with TestClient(api_app) as client:
        response = client.put(
            "/api/playlists/Set/tracks/42-1/rating",
            json={"rating": 6},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 422
    assert source.read_bytes() == original


def test_crate_rating_endpoint_rejects_hardlinked_audio_without_writing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    crates, playlist = playlist_fixture(tmp_path, monkeypatch, source)
    hardlink = source.parent / "linked-track.mp3"
    hardlink.hardlink_to(source)
    playlist.write_text(f"#EXTM3U\n{hardlink}\n", encoding="utf-8")
    (crates / "Set.json").write_text(
        json.dumps({"format": 1, "name": "Set", "tracks": [{
            "track_id": "42", "file_path": str(hardlink),
        }]}),
        encoding="utf-8",
    )
    original = source.read_bytes()

    with TestClient(api_app) as client:
        response = client.put(
            "/api/playlists/Set/tracks/42-1/rating",
            json={"rating": 4},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 400
    assert source.read_bytes() == original


def test_crate_rating_endpoint_rejects_untrusted_origin_without_writing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import app as api_app

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    playlist_fixture(tmp_path, monkeypatch, source)
    original = source.read_bytes()

    with TestClient(api_app) as client:
        response = client.put(
            "/api/playlists/Set/tracks/42-1/rating",
            json={"rating": 4},
            headers={"Origin": "https://attacker.example"},
        )

    assert response.status_code == 403
    assert source.read_bytes() == original


def test_rekordbox_rating_endpoint_rejects_unknown_track(monkeypatch):
    from fastmcp.exceptions import ToolError
    from fastapi.testclient import TestClient
    from api.app import app as api_app
    from api.collection import collection_mcp

    calls = []

    async def call_tool(name, arguments=None):
        calls.append(name)
        raise ToolError("Track with ID 999 not found")

    monkeypatch.setattr(collection_mcp, "call_tool", call_tool)
    with TestClient(api_app) as client:
        response = client.put(
            "/api/collection/tracks/999/rating",
            json={"rating": 3},
            headers={"Origin": "http://localhost:4444"},
        )

    assert response.status_code == 404
    assert calls == ["get_track_file_path"]


def test_rekordbox_playlist_rating_prefers_the_embedded_audio_rating(tmp_path, monkeypatch):
    from api import rekordbox_playlists
    from api.collection import collection_mcp

    source = tmp_path / "music" / "track.mp3"
    source.parent.mkdir()
    synthetic_mp3(source)
    artwork_write.set_audio_rating(source, 5)

    async def call_tool(name, arguments=None):
        assert name == "get_playlist_detail"
        assert arguments == {"playlist_id": "playlist-1"}
        return {"id": "playlist-1", "revision": "revision", "editable": True, "tracks": [
            {"id": "track-1", "playlist_entry_id": "entry-1", "rating": 1, "title": "Test track",
             "file_path": str(source)},
        ]}

    monkeypatch.setattr(rekordbox_playlists, "call_tool", call_tool)
    collection_mcp.track_paths.pop("track-1", None)

    tracks = __import__("asyncio").run(rekordbox_playlists.get_rekordbox_playlist_tracks("playlist-1"))

    collection_mcp.track_paths.pop("track-1", None)
    assert tracks[0]["rating"] == 5
    assert tracks[0]["id"] == "entry-1"
    assert "file_path" not in tracks[0]
