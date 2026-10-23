#!/usr/bin/env python3
"""mrpack-bump: update every mod in a .mrpack to its latest compatible version.

Give it a Modrinth pack file and it gives you back a new .mrpack with
every mod bumped to the newest release that still supports the pack's
game version and loader. Overrides, external files, pack metadata and
your choices stay untouched; only the mod entries change.

No dependencies. Python 3.9+.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API_BASE = "https://api.modrinth.com"
USER_AGENT = "mrpack-bump/0.1.0 (https://github.com/TheRealAlexxx/mrpack-bump)"
VERSION = "0.2.0"

CDN_VERSION_RE = re.compile(
    r"^https?://cdn\.modrinth\.com/data/(?P<project>[A-Za-z0-9]+)/versions/(?P<version>[A-Za-z0-9]+)/"
)

# pack dependency keys -> Modrinth API loader names (the version filter uses the
# API names, while modrinth.index.json pins fabric-loader / quilt-loader)
DEP_KEY_TO_LOADER = {
    "forge": "forge",
    "neoforge": "neoforge",
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
    "liteloader": "liteloader",
    "rift": "rift",
    "risugami": "risugami",
}
LOADER_TO_DEP_KEY = {
    "forge": "forge",
    "neoforge": "neoforge",
    "fabric": "fabric-loader",
    "quilt": "quilt-loader",
}


class BumpError(Exception):
    pass


def validate_member_name(name):
    """Reject zip member names that could escape the extraction directory.

    A .mrpack is a zip. Member names come from whoever built the pack, and
    while launchers validate them, this tool re-writes packs: without a
    check it would faithfully copy absolute paths or ../ traversal names
    into the output pack for a naive extractor to trip over later.
    """
    parts = name.replace("\\", "/").split("/")
    if (
        name.startswith("/")
        or (len(name) > 1 and name[1] == ":")
        or ".." in parts
    ):
        raise BumpError("unsafe member name in pack: %s" % name)


# ---------------------------------------------------------------------------
# pack reading


def read_pack(path):
    """Return (index dict, {name: bytes} for every other zip member)."""
    try:
        archive = zipfile.ZipFile(path)
    except FileNotFoundError:
        raise BumpError("no such file: %s" % path) from None
    except zipfile.BadZipFile as exc:
        raise BumpError("%s is not a valid .mrpack (zip) file" % path) from exc
    with archive:
        try:
            index = json.loads(archive.read("modrinth.index.json").decode("utf-8"))
        except KeyError:
            raise BumpError("%s has no modrinth.index.json - not a Modrinth pack" % path)
        except json.JSONDecodeError as exc:
            raise BumpError("modrinth.index.json is not valid JSON: %s" % exc)
        others = {}
        for name in archive.namelist():
            if name != "modrinth.index.json" and not name.endswith("/"):
                validate_member_name(name)
                others[name] = archive.read(name)
    return index, others


def pack_game_version(index):
    return (index.get("dependencies") or {}).get("minecraft")


def pack_loaders(index):
    deps = index.get("dependencies") or {}
    return [loader for key, loader in DEP_KEY_TO_LOADER.items() if key in deps]


def entry_project_version(entry):
    """(project_id, version_id) from a file entry's Modrinth CDN download URL."""
    for url in entry.get("downloads") or []:
        match = CDN_VERSION_RE.match(url)
        if match:
            return match.group("project"), match.group("version")
    return None, None


# ---------------------------------------------------------------------------
# Modrinth API


class ModrinthClient:
    def __init__(self, api_base=API_BASE, opener=None):
        self.api_base = api_base
        self.opener = opener or urllib.request.build_opener()

    def _get(self, path, params=None):
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BumpError("Modrinth API error %s for %s" % (exc.code, path)) from exc
        except urllib.error.URLError as exc:
            raise BumpError("could not reach api.modrinth.com: %s" % exc.reason) from exc

    def versions_for(self, project_id, game_version, loaders):
        params = {}
        if game_version:
            params["game_versions"] = json.dumps([game_version])
        if loaders:
            params["loaders"] = json.dumps(loaders)
        return self._get("/v2/project/%s/version" % project_id, params)

    def version_files(self, version_id):
        return self._get("/v2/version/%s" % version_id)


CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}


def pick_version(versions, channel):
    """Newest version at or better than the requested channel."""
    allowed = {name for name, rank in CHANNEL_RANK.items() if rank <= CHANNEL_RANK[channel]}
    candidates = [v for v in versions if v.get("version_type") in allowed]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.get("date_published", ""))


def pick_file(version, wanted_filename=None):
    """The primary file of a version, else the file matching the old name."""
    files = version.get("files") or []
    primary = [f for f in files if f.get("primary")]
    if len(primary) == 1:
        return primary[0]
    if wanted_filename:
        for f in files:
            if f.get("filename") == wanted_filename:
                return f
    if primary:
        return primary[0]
    return files[0] if files else None


# ---------------------------------------------------------------------------
# bumping


def bump_entry(entry, client, game_version, loaders, channel, log):
    """Update one file entry in place. Returns a status string."""
    project_id, version_id = entry_project_version(entry)
    if not project_id:
        log("skip     %s (no Modrinth download URL, cannot update)" % entry.get("path"))
        return "external"
    old_filename = (entry.get("path") or "").rsplit("/", 1)[-1]
    versions = client.versions_for(project_id, game_version, loaders)
    chosen = pick_version(versions, channel)
    if not chosen:
        log("keep     %s (no %s version for this game version/loader)" % (old_filename, channel))
        return "blocked"
    if chosen.get("id") == version_id:
        log("current  %s (%s)" % (old_filename, chosen.get("version_number")))
        return "current"
    file_info = pick_file(chosen, old_filename)
    if not file_info:
        log("keep     %s (new version %s has no files)" % (old_filename, chosen.get("version_number")))
        return "blocked"
    new_filename = file_info.get("filename") or old_filename
    directory = (entry.get("path") or "").rsplit("/", 1)[0] if "/" in (entry.get("path") or "") else ""
    entry["path"] = (directory + "/" + new_filename) if directory else new_filename
    entry["downloads"] = [file_info["url"]] if file_info.get("url") else []
    entry["hashes"] = {k: v for k, v in (file_info.get("hashes") or {}).items() if k in ("sha1", "sha512")}
    entry["fileSize"] = file_info.get("size")
    env = entry.get("env")
    log("bump     %s -> %s (%s)" % (old_filename, new_filename, chosen.get("version_number")))
    if env is not None:
        entry["env"] = env
    return "bumped"


def migrate_dependencies(index, game_version=None, loaders=None, loader_versions=None):
    """Rewrite the pack's dependency pins for a migration. Returns change lines."""
    deps = dict(index.get("dependencies") or {})
    changes = []
    if game_version and deps.get("minecraft") != game_version:
        changes.append("minecraft %s -> %s" % (deps.get("minecraft"), game_version))
        deps["minecraft"] = game_version
    if loaders is not None:
        loader_versions = loader_versions or {}
        old = ["%s %s" % (key, deps[key]) for key in DEP_KEY_TO_LOADER if key in deps]
        for key in DEP_KEY_TO_LOADER:
            deps.pop(key, None)
        new = []
        for loader in loaders:
            version = loader_versions.get(loader)
            if not version:
                raise BumpError(
                    "migrating to %s needs its loader version: pass --loader-version %s=VERSION"
                    % (loader, loader))
            dep_key = LOADER_TO_DEP_KEY.get(loader, loader)
            deps[dep_key] = version
            new.append("%s %s" % (dep_key, version))
        changes.append("loaders %s -> %s" % (", ".join(old) or "none", ", ".join(new)))
    index["dependencies"] = deps
    return changes


def bump_pack(index, client, game_version=None, loaders=None, channel="release", log=print):
    """Bump every updatable file entry. Returns a status tally."""
    game_version = game_version or pack_game_version(index)
    loaders = loaders if loaders is not None else pack_loaders(index)
    tally = {"bumped": 0, "current": 0, "blocked": 0, "external": 0}
    for entry in index.get("files") or []:
        status = bump_entry(entry, client, game_version, loaders, channel, log)
        tally[status] += 1
    return tally


def write_pack(path, index, others):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("modrinth.index.json", json.dumps(index, indent=2))
        for name, data in others.items():
            validate_member_name(name)
            archive.writestr(name, data)


# ---------------------------------------------------------------------------
# CLI


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mrpack-bump",
        description="Update every mod in a .mrpack to its latest compatible version.",
    )
    parser.add_argument("pack", help="input .mrpack file")
    parser.add_argument("-o", "--output", help="output .mrpack path (default: <pack>.bumped.mrpack, or the input path with --in-place)")
    parser.add_argument("--in-place", action="store_true", help="rewrite the input pack")
    parser.add_argument("--game-version", help="target a different Minecraft version than the pack's own")
    parser.add_argument("--loader", action="append", help="migrate the pack to this loader, repeatable")
    parser.add_argument("--loader-version", action="append", metavar="LOADER=VERSION",
                        help="version pin for each loader given with --loader, repeatable")
    parser.add_argument("--channel", choices=list(CHANNEL_RANK), default="release",
                        help="newest channel to accept (default: release)")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = parser.parse_args(argv)

    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))
    try:
        loader_versions = {}
        for item in args.loader_version or []:
            if "=" not in item:
                raise BumpError("--loader-version must be LOADER=VERSION, got %r" % item)
            key, _, value = item.partition("=")
            loader_versions[key.strip()] = value.strip()
        index, others = read_pack(args.pack)
        game_version = args.game_version or pack_game_version(index)
        loaders = args.loader if args.loader is not None else pack_loaders(index)
        if not game_version:
            log("warning: the pack declares no Minecraft version; checking every game version")
        if not loaders:
            log("warning: the pack declares no loader; checking every loader")
        pack_label = " ".join(
            part for part in (index.get("name"), index.get("versionId")) if part
        ) or "(unnamed pack)"
        log("pack: %s | game %s | loaders %s | %d files" % (
            pack_label, game_version or "any",
            ",".join(loaders) or "any", len(index.get("files") or [])))
        client = ModrinthClient()
        tally = bump_pack(index, client, game_version, loaders, args.channel, log)
        migrating = args.loader is not None or (
            args.game_version and args.game_version != pack_game_version(index))
        changes = migrate_dependencies(
            index,
            game_version=args.game_version or None,
            loaders=args.loader,
            loader_versions=loader_versions,
        ) if migrating else []
        for change in changes:
            log("migrate  %s" % change)
    except BumpError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    output = args.output
    if args.in_place:
        output = args.pack
    if not output:
        output = args.pack.rsplit(".mrpack", 1)[0] + ".bumped.mrpack"
    if not args.dry_run and (tally["bumped"] or changes):
        write_pack(output, index, others)
    elif args.dry_run:
        output = None

    summary = {
        "bumped": tally["bumped"], "current": tally["current"],
        "blocked": tally["blocked"], "external": tally["external"],
        "output": output,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("%d bumped, %d already current, %d kept (no compatible version), %d external%s" % (
            summary["bumped"], summary["current"], summary["blocked"], summary["external"],
            (" -> " + output) if output else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
