# nix-pins

Version pin tracking tooling for Nix configurations — a declarative manifest
drives a CLI that compares pinned versions against upstream, evaluates overlays
to extract source metadata, and flags entries ready for deletion.

## Why This Exists

~25 divergences from nixpkgs (overlays, custom Darwin packages, pinned flake
inputs) are invisible without a dashboard. `nix-pins` builds one by *evaluating
the Nix expressions themselves* — no external dependencies, no duplicated URL
templates.

## Structure

```
flake.nix       Standalone flake — exposes the `nix-pins` output (report /
                src / drv / overlays / applied / installed).
eval.nix        The evaluation half — pure Nix, never composes expressions
                in strings. Exposed as the flake output `nix-pins`.
manifest.nix    Declarative manifest — every hand-written Entry (overlay /
                custom package / pinned flake input) that this repo
                Maintains on top of nixpkgs.
nix-pins.py     The CLI — dashboard, pin-bump, sync, disable/enable.
overlays/auto/  Auto Pins — data files that are themselves the entry;
                no manifest row needed.
```

## How It Works

1. `manifest.nix` declares every tracked unit (Entry) with its source, target
   nixpkgs attribute, system, and — for overlays — an `enable` field that
   decides wiring.

2. `eval.nix` evaluates the full Nix expressions, extracting version strings,
   hashes, and derivations without shell-out or string composition.

3. `nix-pins.py` (Python) does HTTP (upstream version fetch) and file
   rewriting, driven by the manifest and the evaluation results.

### The Purpose Taxonomy

Each Entry has a `purpose` that drives deletion decisions:

- **version-lag** — outpaces a lagging nixpkgs; droppable once nixpkgs
  catches up (dashboard shows `DROP?` when nixpkgs version ≥ pin).

- **compat** — makes the package work at all (platform/build fix); NOT
  droppable on version parity (dashboard shows `REVIEW / stillNeeded`).

- **packaging** — we are the packager; no nixpkgs/brew equivalent.

## Quick Start (standalone)

```bash
# Dashboard: check all pins against upstream
./nix-pins.py

# Dashboard + which hosts use each entry (requires host config evaluation)
./nix-pins.py check --installed

# Bump a single pin (fetches upstream, prefetches, verifies build)
./nix-pins.py update <name>

# Disable an entry (sets enable = false in manifest)
./nix-pins.py disable <name>

# Interactive sync (review warnings, bulk-apply updates)
./nix-pins.py sync
```

### Usage from a user repo (dotnix)

When a user repo contains `nix-pins/` at the root (same flat structure), set:

```bash
NIX_PINS_DIR=./nix-pins ./nix-pins.py check --installed
```

The CLI resolves `REPO` from the env var and defaults to the parent
directory when unset — enabling system-wide installation alongside a local
clone.

## Notes

- Auto Pins (`overlays/auto/<token>.json`) are Homebrew cask pins that trust
  Homebrew's trust model: `url + sha256` come from `formulae.brew.sh`, and
  `nix build` verifies the artifact against that hash.
- `stillNeeded` predicates are real Nix functions (`pkgs: bool`) that answer
  whether an overlay is still required after nixpkgs catches up.
- The dashboard exit codes: `0` = clean, `1` = outdated / drift (file as
  issue), `2` = broken manifest or tool (fail the check).
