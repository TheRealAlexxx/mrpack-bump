import json
import os
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mrpack_bump as mb


class FakeClient(mb.ModrinthClient):
    """ModrinthClient that never touches the network."""

    def __init__(self, versions_by_project):
        self.versions_by_project = versions_by_project
        self.calls = []

    def versions_for(self, project_id, game_version, loaders):
        self.calls.append((project_id, game_version, loaders))
        return self.versions_by_project.get(project_id, [])


def make_version(vid, number, date, vtype="release", filename="mod.jar", url=None):
    return {
        "id": vid, "version_number": number, "date_published": date,
        "version_type": vtype,
        "files": [{
            "filename": filename, "primary": True,
            "url": url or ("https://cdn.modrinth.com/data/PROJ/versions/%s/%s" % (vid, filename)),
            "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
            "size": 12345,
        }],
    }


def make_entry(path="mods/example.jar", project="PROJ", version="V1", env=None):
    entry = {
        "path": path,
        "hashes": {"sha1": "0" * 40, "sha512": "0" * 128},
        "downloads": ["https://cdn.modrinth.com/data/%s/versions/%s/%s" % (project, version, path.rsplit("/", 1)[-1])],
        "fileSize": 100,
    }
    if env:
        entry["env"] = env
    return entry


def make_pack(tmp_path, index, extras=None):
    pack = tmp_path / "test.mrpack"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("modrinth.index.json", json.dumps(index))
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
    return pack


BASE_INDEX = {
    "formatVersion": 1, "game": "minecraft", "name": "Test Pack", "versionId": "1.0.0",
    "dependencies": {"minecraft": "1.20.1", "forge": "47.2.0"},
}


# ---- parsing ---------------------------------------------------------------

def test_read_pack_round_trip(tmp_path):
    index = dict(BASE_INDEX, files=[])
    pack = make_pack(tmp_path, index, {"overrides/config/x.cfg": b"hello"})
    got_index, others = mb.read_pack(str(pack))
    assert got_index["name"] == "Test Pack"
    assert others["overrides/config/x.cfg"] == b"hello"


def test_read_pack_rejects_non_zip(tmp_path):
    bad = tmp_path / "bad.mrpack"
    bad.write_bytes(b"not a zip")
    with pytest.raises(mb.BumpError):
        mb.read_pack(str(bad))


def test_game_version_and_loaders():
    assert mb.pack_game_version(BASE_INDEX) == "1.20.1"
    assert mb.pack_loaders(BASE_INDEX) == ["forge"]
    assert mb.pack_loaders({"dependencies": {"minecraft": "1.21.1", "neoforge": "21"}}) == ["neoforge"]


def test_entry_project_version():
    entry = make_entry(project="AABBCCDD", version="VVVV2222")
    assert mb.entry_project_version(entry) == ("AABBCCDD", "VVVV2222")
    external = {"path": "mods/x.jar", "downloads": ["https://files.example.com/x.jar"]}
    assert mb.entry_project_version(external) == (None, None)


# ---- version picking -------------------------------------------------------

def test_pick_version_prefers_newest_release():
    versions = [
        make_version("V1", "1.0", "2026-01-01T00:00:00Z"),
        make_version("V2", "2.0-beta", "2026-06-01T00:00:00Z", "beta"),
        make_version("V3", "1.1", "2026-03-01T00:00:00Z"),
    ]
    assert mb.pick_version(versions, "release")["id"] == "V3"
    assert mb.pick_version(versions, "beta")["id"] == "V2"


def test_pick_version_none_when_channel_too_strict():
    versions = [make_version("V2", "2.0-beta", "2026-06-01T00:00:00Z", "beta")]
    assert mb.pick_version(versions, "release") is None


def test_pick_file_primary_then_name_match():
    version = {"files": [
        {"filename": "mod-dev.jar", "primary": False},
        {"filename": "mod.jar", "primary": False},
    ]}
    assert mb.pick_file(version, "mod.jar")["filename"] == "mod.jar"
    version["files"][0]["primary"] = True
    version["files"].append({"filename": "mod-sources.jar", "primary": True})
    # two primaries: fall back to the old filename
    assert mb.pick_file(version, "mod.jar")["filename"] == "mod.jar"


# ---- bumping ---------------------------------------------------------------

def test_bump_updates_entry_fields():
    entry = make_entry()
    new_version = make_version("V2", "2.0", "2026-06-01T00:00:00Z", filename="example-2.0.jar")
    client = FakeClient({"PROJ": [make_version("V1", "1.0", "2026-01-01T00:00:00Z"), new_version]})
    logs = []
    status = mb.bump_entry(entry, client, "1.20.1", ["forge"], "release", logs.append)
    assert status == "bumped"
    assert entry["path"] == "mods/example-2.0.jar"
    assert entry["downloads"] == [new_version["files"][0]["url"]]
    assert entry["hashes"]["sha1"] == "a" * 40
    assert entry["fileSize"] == 12345
    assert client.calls == [("PROJ", "1.20.1", ["forge"])]


def test_bump_keeps_current_version():
    entry = make_entry(version="V1")
    client = FakeClient({"PROJ": [make_version("V1", "1.0", "2026-01-01T00:00:00Z")]})
    before = json.dumps(entry, sort_keys=True)
    assert mb.bump_entry(entry, client, "1.20.1", ["forge"], "release", lambda m: None) == "current"
    assert json.dumps(entry, sort_keys=True) == before


def test_bump_keeps_when_no_compatible_version():
    entry = make_entry()
    client = FakeClient({"PROJ": []})
    assert mb.bump_entry(entry, client, "1.20.1", ["forge"], "release", lambda m: None) == "blocked"


def test_bump_skips_external_downloads():
    entry = {"path": "mods/external.jar", "downloads": ["https://example.com/external.jar"]}
    assert mb.bump_entry(entry, FakeClient({}), "1.20.1", ["forge"], "release", lambda m: None) == "external"


def test_bump_preserves_env():
    entry = make_entry(env={"client": "required", "server": "unsupported"})
    new_version = make_version("V2", "2.0", "2026-06-01T00:00:00Z")
    client = FakeClient({"PROJ": [new_version]})
    mb.bump_entry(entry, client, "1.20.1", ["forge"], "release", lambda m: None)
    assert entry["env"] == {"client": "required", "server": "unsupported"}


def test_bump_pack_tally():
    index = dict(BASE_INDEX, files=[
        make_entry("mods/a.jar", "PA", "V1"),
        make_entry("mods/b.jar", "PB", "V9"),
        make_entry("mods/c.jar", "PC", "V1"),
        {"path": "mods/d.jar", "downloads": ["https://example.com/d.jar"]},
    ])
    client = FakeClient({
        "PA": [make_version("V2", "2.0", "2026-06-01T00:00:00Z", filename="a-2.0.jar")],
        "PB": [make_version("V9", "9.0", "2026-01-01T00:00:00Z", filename="b.jar")],
        "PC": [],
    })
    tally = mb.bump_pack(index, client, log=lambda m: None)
    assert tally == {"bumped": 1, "current": 1, "blocked": 1, "external": 1}


def test_write_pack_keeps_overrides(tmp_path):
    index = dict(BASE_INDEX, files=[make_entry()])
    pack = make_pack(tmp_path, index, {"overrides/options.txt": b"fov:90"})
    got_index, others = mb.read_pack(str(pack))
    out = tmp_path / "out.mrpack"
    mb.write_pack(str(out), got_index, others)
    with zipfile.ZipFile(out) as archive:
        assert archive.read("overrides/options.txt") == b"fov:90"
        assert json.loads(archive.read("modrinth.index.json"))["name"] == "Test Pack"


# ---- CLI -------------------------------------------------------------------

def run_cli(*argv):
    return subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mrpack_bump.py"), *argv],
        capture_output=True, text=True)


def test_cli_rejects_bad_pack(tmp_path):
    bad = tmp_path / "bad.mrpack"
    bad.write_bytes(b"garbage")
    result = run_cli(str(bad))
    assert result.returncode == 2
    assert "error:" in result.stderr
