"""Unpacking an uploaded export zip. The zip is untrusted, so most of these try
to get something out of the bundle folder or past the limits."""
import io
import stat
import struct
import threading
import zipfile
from pathlib import Path

import pytest

from app.services.imports import unpack
from app.services.imports.unpack import UnpackError, unpack_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "bundle"
BIG = 10**9
SERVER_JSON = b'{"format": 1}'


@pytest.fixture(autouse=True)
def plenty_of_room(monkeypatch):
    monkeypatch.setattr(unpack, "free_bytes", lambda path: 10**15)


def make_zip(tmp_path: Path, entries, name: str = "export.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, data in entries:
            archive.writestr(entry, data)
    return path


def run(tmp_path, entries, **limits) -> Path:
    archive = entries if isinstance(entries, Path) else make_zip(tmp_path, entries)
    dest = tmp_path / "out" / "bundle"
    unpack_bundle(archive, dest, **{"max_unpacked": BIG, "max_file": BIG, **limits})
    return dest


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def fixture_entries(prefix: str = ""):
    return [(prefix + p.relative_to(FIXTURE).as_posix(), p.read_bytes())
            for p in sorted(FIXTURE.rglob("*")) if p.is_file()]


def fixture_tree() -> set[str]:
    return {p for p in tree(FIXTURE) if not p.startswith(("avatars/", "emoji/"))}


def patch_header(archive: Path, name: str, offset: int, fmt: str, value) -> None:
    """Change a field of an entry's central directory header."""
    data = bytearray(archive.read_bytes())
    at = data.find(b"PK\x01\x02")
    while at != -1:
        name_length = struct.unpack_from("<H", data, at + 28)[0]
        if bytes(data[at + 46:at + 46 + name_length]) == name.encode():
            struct.pack_into(fmt, data, at + offset, value)
            archive.write_bytes(bytes(data))
            return
        at = data.find(b"PK\x01\x02", at + 4)
    raise AssertionError(f"{name} not in the central directory")


def entry(name: str, mode: int | None = None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    if mode is not None:
        info.external_attr = mode << 16
    return info


def test_a_bundle_at_the_root(tmp_path):
    dest = run(tmp_path, fixture_entries())

    assert tree(dest) == fixture_tree()
    assert (dest / "server.json").read_bytes() == (FIXTURE / "server.json").read_bytes()
    assert (dest / "files/9001/cat.png").read_bytes() == (FIXTURE / "files/9001/cat.png").read_bytes()


def test_a_bundle_inside_one_folder(tmp_path):
    dest = run(tmp_path, fixture_entries("my export/"))

    assert tree(dest) == fixture_tree()


def test_the_shallowest_server_json_is_the_bundle(tmp_path):
    dest = run(tmp_path, [
        ("inner/server.json", b'{"inner": true}'),
        ("server.json", SERVER_JSON),
        ("channels/1/messages/1.json", b"[]"),
        ("inner/channels/2/messages/1.json", b"[]"),
    ])

    assert tree(dest) == {"server.json", "channels/1/messages/1.json"}
    assert (dest / "server.json").read_bytes() == SERVER_JSON


def test_backslashes_separate_folders(tmp_path):
    dest = run(tmp_path, [
        ("export\\server.json", SERVER_JSON),
        ("export\\channels\\1\\messages\\1.json", b"[]"),
        ("export\\files\\7\\a b.txt", b"hi"),
    ])

    assert tree(dest) == {"server.json", "channels/1/messages/1.json", "files/7/a b.txt"}


@pytest.mark.parametrize("entries", [
    [("channels/1/messages/1.json", b"[]")],
    [("a/b/server.json", SERVER_JSON)],
    [("server.json/", b"")],
    [],
])
def test_a_zip_without_server_json_is_refused(tmp_path, entries):
    with pytest.raises(UnpackError, match="no server.json"):
        run(tmp_path, entries)
    assert not (tmp_path / "out" / "bundle").exists()


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    for content in (b"just some text", b"", b"PK\x03\x04" + b"x" * 40):
        path = tmp_path / "not.zip"
        path.write_bytes(content)
        with pytest.raises(UnpackError, match="This file is not a zip archive"):
            run(tmp_path, path)


def test_a_cut_off_zip_is_refused(tmp_path):
    whole = make_zip(tmp_path, fixture_entries(), "whole.zip").read_bytes()
    cut = tmp_path / "cut.zip"
    cut.write_bytes(whole[:len(whole) // 2])

    with pytest.raises(UnpackError, match="not a zip archive"):
        run(tmp_path, cut)


def test_unsafe_names_are_skipped_and_nothing_lands_outside(tmp_path):
    run(tmp_path, [
        ("server.json", SERVER_JSON),
        ("../server.json", b"outside"),
        ("../../escape.json", b"outside"),
        ("/etc/server.json", b"outside"),
        ("/channels/1/messages/1.json", b"outside"),
        ("C:/channels/1/messages/1.json", b"outside"),
        ("c:channels/1/messages/2.json", b"outside"),
        ("channels/../../escape/messages/1.json", b"outside"),
        ("channels/1/messages/../../../escape.json", b"outside"),
        ("channels/./1/messages/3.json", b"outside"),
        ("channels//1/messages/4.json", b"outside"),
        ("files/..", b"outside"),
        ("files/1/..", b"outside"),
        ("files/../2/x", b"outside"),
        ("files/1/ok.txt", b"fine"),
    ])

    assert tree(tmp_path / "out") == {"bundle/server.json", "bundle/files/1/ok.txt"}
    assert tree(tmp_path) == {"export.zip", "out/bundle/server.json", "out/bundle/files/1/ok.txt"}


def test_a_symlink_entry_is_skipped(tmp_path):
    archive = tmp_path / "links.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("server.json", SERVER_JSON)
        zf.writestr(entry("channels/1/messages/1.json", stat.S_IFLNK | 0o777), "/etc/passwd")
        zf.writestr(entry("files/1/link", stat.S_IFLNK | 0o777), "../../../secret")
        zf.writestr(entry("files/1/device", stat.S_IFCHR | 0o666), "")
        zf.writestr(entry("files/1/plain", stat.S_IFREG | 0o644), "data")

    dest = run(tmp_path, archive)

    assert tree(dest) == {"server.json", "files/1/plain"}
    assert not any(p.is_symlink() for p in dest.rglob("*"))


def test_files_a_bundle_does_not_have_are_ignored(tmp_path):
    long_id = "x" * 65
    dest = run(tmp_path, [
        ("server.json", SERVER_JSON),
        ("progress.json", b"{}"),
        ("profiles.json", b"{}"),
        ("avatars/a1.png", b"png"),
        ("emoji/e1.png", b"png"),
        ("README.txt", b"hi"),
        ("channels/1/notes.txt", b"x"),
        ("channels/1/messages/first.json", b"[]"),
        ("channels/1/messages/1.txt", b"[]"),
        ("channels/1/messages/-1.json", b"[]"),
        ("channels/1/messages/" + "9" * 19 + ".json", b"[]"),
        ("channels/1/threads/.json", b"[]"),
        ("channels/1/threads/...json", b"[]"),
        ("channels/1/other/1.json", b"[]"),
        ("channels/1/messages/sub/1.json", b"[]"),
        ("channels/a b/messages/1.json", b"[]"),
        (f"channels/{long_id}/messages/1.json", b"[]"),
        ("channels/1.json", b"[]"),
        ("files/1", b"x"),
        ("files/1/a/b.txt", b"x"),
        ("files/a b/x.txt", b"x"),
        ("files/1/" + "n" * 256, b"x"),
        ("files/1/" + "é" * 128, b"x"),
        ("channels/1/messages/12.json", b"[1]"),
        ("channels/c-1.x/threads/t_9.json", b"{}"),
        ("files/1/" + "n" * 255, b"x"),
        ("files/" + "i" * 64 + "/résumé.pdf", b"x"),
    ])

    assert tree(dest) == {
        "server.json", "channels/1/messages/12.json", "channels/c-1.x/threads/t_9.json",
        "files/1/" + "n" * 255, "files/" + "i" * 64 + "/résumé.pdf"}


def test_a_later_duplicate_does_not_replace_the_first(tmp_path):
    dest = run(tmp_path, [
        ("server.json", b"first"),
        ("server.json", b"second"),
        ("files/1/a.txt", b"one"),
        ("files\\1\\a.txt", b"two"),
    ])

    assert (dest / "server.json").read_bytes() == b"first"
    assert (dest / "files/1/a.txt").read_bytes() == b"one"


def test_an_entry_longer_than_it_declares_fails(tmp_path):
    archive = make_zip(tmp_path, [
        ("server.json", SERVER_JSON), ("files/1/big.bin", b"0123456789" * 100)])
    patch_header(archive, "files/1/big.bin", 24, "<I", 20)

    with pytest.raises(UnpackError):
        run(tmp_path, archive)
    assert not (tmp_path / "out" / "bundle").exists()


def test_the_copy_stops_at_the_declared_size_even_if_the_archive_yields_more(tmp_path):
    info = zipfile.ZipInfo("files/1/big.bin")
    info.file_size = 10

    class Archive:
        def open(self, _):
            return io.BytesIO(b"x" * 11)

    dest = tmp_path / "dest"
    dest.mkdir()
    writer = unpack._Writer(Archive(), dest, 10, BIG, None, None)
    with pytest.raises(UnpackError, match="larger than it says"):
        writer.copy(unpack._Entry(info, ("files", "1", "big.bin"), is_json=False))


def test_a_data_file_over_the_cap_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(unpack, "MAX_JSON_BYTES", 100)

    with pytest.raises(UnpackError, match="data file"):
        run(tmp_path, [("server.json", SERVER_JSON), ("channels/1/messages/1.json", b"x" * 101)])
    assert not (tmp_path / "out" / "bundle").exists()


def test_a_data_file_that_grows_past_the_cap_while_copying_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(unpack, "MAX_JSON_BYTES", 100)
    info = zipfile.ZipInfo("server.json")
    info.file_size = 5000

    class Archive:
        def open(self, _):
            return io.BytesIO(b"x" * 5000)

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(UnpackError, match="data file"):
        unpack._Writer(Archive(), dest, 5000, BIG, None, None).copy(
            unpack._Entry(info, ("server.json",), is_json=True))


def test_the_unpacked_total_has_a_limit(tmp_path):
    entries = [("server.json", SERVER_JSON), ("files/1/a", b"a" * 600), ("files/1/b", b"b" * 600)]

    with pytest.raises(UnpackError, match="allowed unpacked size"):
        run(tmp_path, entries, max_unpacked=1000)
    assert not (tmp_path / "out" / "bundle").exists()
    assert tree(run(tmp_path, entries, max_unpacked=1300)) == {"server.json", "files/1/a", "files/1/b"}


def test_files_that_are_skipped_do_not_count_against_the_limit(tmp_path):
    entries = [
        ("server.json", SERVER_JSON), ("avatars/a.png", b"a" * 5000), ("files/1/a", b"a" * 50)]

    assert tree(run(tmp_path, entries, max_unpacked=500)) == {"server.json", "files/1/a"}


def test_too_many_entries_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(unpack, "MAX_ENTRIES", 2)

    with pytest.raises(UnpackError, match="too many files"):
        run(tmp_path, [("server.json", SERVER_JSON), ("a", b""), ("b", b"")])
    assert not (tmp_path / "out" / "bundle").exists()


def test_an_absurd_central_directory_is_refused_before_it_is_read(tmp_path, monkeypatch):
    archive = make_zip(tmp_path, [("server.json", SERVER_JSON)])
    data = bytearray(archive.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<I", data, end + 12, 0x7FFFFFFF)
    archive.write_bytes(bytes(data))

    def never(*args, **kwargs):
        raise AssertionError("the archive was opened")

    monkeypatch.setattr(zipfile, "ZipFile", never)
    with pytest.raises(UnpackError, match="too many files"):
        run(tmp_path, archive)


def test_an_encrypted_entry_is_refused(tmp_path):
    archive = make_zip(tmp_path, [("server.json", SERVER_JSON), ("avatars/a.png", b"secret")])
    patch_header(archive, "avatars/a.png", 8, "<H", 0x1)

    with pytest.raises(UnpackError, match="password"):
        run(tmp_path, archive)
    assert not (tmp_path / "out" / "bundle").exists()


def test_an_attachment_over_the_size_limit_is_not_written(tmp_path):
    dest = run(tmp_path, [
        ("server.json", SERVER_JSON), ("files/1/big.bin", b"x" * 50),
        ("files/1/small.bin", b"x" * 10), ("channels/1/messages/1.json", b"y" * 500),
    ], max_file=10)

    assert tree(dest) == {"server.json", "files/1/small.bin", "channels/1/messages/1.json"}


def test_a_failure_removes_what_was_written(tmp_path):
    archive = make_zip(tmp_path, [
        ("server.json", SERVER_JSON), ("files/1/a", b"a" * 100), ("files/1/b", b"b" * 100)])
    stop = threading.Event()

    with pytest.raises(UnpackError, match="stopped"):
        unpack_bundle(
            archive, tmp_path / "out" / "bundle", max_unpacked=BIG, max_file=BIG, stop=stop,
            progress=lambda done, total: stop.set() if done >= 100 else None)

    assert not (tmp_path / "out" / "bundle").exists()


def test_a_damaged_entry_fails_and_leaves_nothing(tmp_path):
    archive = make_zip(tmp_path, [("server.json", SERVER_JSON), ("files/1/a", b"a" * 5000)])
    data = bytearray(archive.read_bytes())
    at = data.find(b"files/1/a") + len("files/1/a")
    data[at + 10:at + 20] = b"\xff" * 10
    archive.write_bytes(bytes(data))

    with pytest.raises(UnpackError, match="damaged"):
        run(tmp_path, archive)
    assert not (tmp_path / "out" / "bundle").exists()


def test_progress_counts_the_bytes_written(tmp_path):
    seen = []
    archive = make_zip(tmp_path, [
        ("server.json", SERVER_JSON), ("files/1/a", b"a" * 300), ("avatars/a.png", b"z" * 999)])

    unpack_bundle(archive, tmp_path / "bundle", max_unpacked=BIG, max_file=BIG,
                  progress=lambda done, total: seen.append((done, total)))

    total = len(SERVER_JSON) + 300
    assert seen[-1] == (total, total)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


def test_not_enough_disk_space_is_refused_with_a_margin(tmp_path, monkeypatch):
    entries = [("server.json", SERVER_JSON), ("files/1/a", b"a" * 1000)]
    needed = len(SERVER_JSON) + 1000
    monkeypatch.setattr(unpack, "free_bytes", lambda path: unpack.FREE_SPACE_MARGIN + needed)
    assert tree(run(tmp_path, entries)) == {"server.json", "files/1/a"}

    monkeypatch.setattr(unpack, "free_bytes", lambda path: unpack.FREE_SPACE_MARGIN + needed - 1)
    with pytest.raises(UnpackError, match="Not enough disk space to unpack this export"):
        unpack_bundle(make_zip(tmp_path, entries), tmp_path / "other", max_unpacked=BIG, max_file=BIG)
    assert not (tmp_path / "other").exists()


def test_a_symlink_already_in_the_destination_cannot_redirect_a_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "bundle"
    dest.mkdir()
    (dest / "files").symlink_to(outside, target_is_directory=True)
    archive = make_zip(tmp_path, [("server.json", SERVER_JSON), ("files/1/a.txt", b"x")])

    with pytest.raises(UnpackError):
        unpack_bundle(archive, dest, max_unpacked=BIG, max_file=BIG)

    assert list(outside.iterdir()) == []
