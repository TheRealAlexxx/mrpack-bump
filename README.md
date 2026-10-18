# mrpack-bump

Update every mod in a `.mrpack` to its latest compatible version, in one command.

Hand it a Modrinth pack file and it hands you back a new pack with every
mod bumped to the newest release that still supports the pack's game
version and loader. Overrides, external files, environment flags and
pack metadata stay exactly as they were; only the mod entries move.

## Why

Modpack maintenance is mostly one chore: bumping every mod. packwiz can
do it if your pack lives in packwiz format, ferium if it lives in a
ferium profile, and a few report-only tools will tell you an update
exists. Nothing takes an arbitrary `.mrpack` file and gives you an
updated `.mrpack` back. This does.

## Install

No dependencies. Python 3.9+.

```sh
git clone https://github.com/TheRealAlexxx/mrpack-bump
cd mrpack-bump
python3 mrpack_bump.py --help
```

## Usage

```sh
python3 mrpack_bump.py mypack.mrpack                  # writes mypack.bumped.mrpack
python3 mrpack_bump.py mypack.mrpack --in-place       # rewrite the input
python3 mrpack_bump.py mypack.mrpack --dry-run        # report only
python3 mrpack_bump.py mypack.mrpack --channel beta   # allow beta versions
python3 mrpack_bump.py mypack.mrpack --game-version 1.21.1   # retarget a game version
python3 mrpack_bump.py mypack.mrpack --json           # machine-readable summary
```

Example:

```
pack: Outbreak Zone 1.2.0 | game 1.20.1 | loaders forge | 96 files
bump     jei-1.20.1-forge-15.0.0.12.jar -> jei-1.20.1-forge-15.56.0.205.jar (15.56.0.205)
current  kotlin-for-forge-4.11.0-all.jar (4.11.0)
keep     some-mod-1.2.jar (no release version for this game version/loader)
skip     tissous-zombie-pack.jar (no Modrinth download URL, cannot update)
1 bumped, 1 already current, 1 kept (no compatible version), 1 external -> outbreak-zone.bumped.mrpack
```

## How it works

Each file in `modrinth.index.json` links to a Modrinth CDN download,
which names its project and version. mrpack-bump asks the Modrinth API
for the newest version of that project that still matches the pack's
declared Minecraft version and loader (the `dependencies` block), then
rewrites the entry with the new download URL, hashes and file size.
Entries with non-Modrinth download links are left alone and listed as
external. Mods with no compatible release are kept at their pinned
version and listed, so you can see exactly what blocks an upgrade.

Limits: it updates references, not jars, so the pack must still be
installed by a launcher or installer afterwards. It cannot guess
compatibility for external (non-Modrinth) files. `--game-version` finds
mods for the target version but does not port your configs; test the
bumped pack before shipping it.

## License

MIT
