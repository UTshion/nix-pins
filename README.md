# nix-pins

A dashboard and updater for the divergences a Nix flake maintains on top of
nixpkgs: overlays, custom packages, pinned flake inputs and
[brew-nix](https://github.com/BatteredBunny/brew-nix) casks. A declarative
manifest says *why* each divergence exists, so the tool can tell you not only
"this pin is outdated" but also "this overlay can be deleted now".

## Why this exists

A config that has accumulated ~25 overlays and pins can't be kept current by
eye. nix-pins builds the dashboard by *evaluating the Nix expressions
themselves*: no duplicated URL templates, no string-built Nix, pure evaluation
against the flake's own locked inputs.

## Layout

```
flake.nix         lib.mkNixPins / lib.autoPinOverlays, packages.default (CLI), apps.default
eval.nix          the evaluation half — becomes your flake's `nix-pins` output
auto-pins.nix     Auto Pin loader: <dir>/<token>.json → overlays
nix-pins.py       the CLI — dashboard, update, sync, disable/enable
package.nix       the CLI package (stdlib-only Python, wrapped)
example/manifest.nix  documented schema + example entries
```

## How it works

1. **The manifest** lists every hand-written Entry with its source, target
   nixpkgs attribute, systems and — for overlays — an `enable` field that *is*
   the wiring: your flake applies exactly the enabled overlays.
2. **eval.nix** (`lib.mkNixPins`) turns the manifest into flake attributes:
   versions, `stillNeeded` verdicts, each entry's `src`/`drv`, the overlay
   lists, per-host reachability — and the repo layout the CLI should use.
3. **The CLI** does the rest — HTTP lookups for upstream versions, file
   rewriting, the dashboard — reading everything through `.#nix-pins.*`.

### The Purpose taxonomy

Each Entry has a `purpose` that drives deletion decisions:

| Purpose       | Why the Entry exists                     | Deletable when…                          |
| ------------- | ---------------------------------------- | ---------------------------------------- |
| `version-lag` | nixpkgs lags upstream                    | nixpkgs catches up (`…+DROP?` status)    |
| `compat`      | makes the package work at all            | nixpkgs fixes the underlying problem     |
| `packaging`   | we are the packager; no nixpkgs package  | (never, unless the package is abandoned) |

`stillNeeded` predicates are real Nix functions (`pkgs: bool`) that answer
whether a compat overlay is still required — they catch fixes that land
without a version bump.

## Using it from your flake

```nix
{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    nix-pins = {
      url = "github:UTshion/nix-pins";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nix-pins, ... }:
    let
      nixPins = nix-pins.lib.mkNixPins {
        lib = nixpkgs.lib;
        root = ./.;
        manifest = ./nix-pins/manifest.nix;
        platforms.linux = {
          input = nixpkgs;
          defaultSystem = "x86_64-linux";
        };
        # optional — see the argument list at the top of eval.nix:
        # brewApi, autoPinsDir, caskModule, caskPkgs, hosts, overlaysDir, packagesDir
      };
    in
    {
      # The CLI reads everything through this output.
      nix-pins = nixPins;

      # Same revision as the eval half: run it with `nix run .#nix-pins`.
      packages.x86_64-linux.nix-pins = nix-pins.packages.x86_64-linux.default;

      nixosConfigurations.host = nixpkgs.lib.nixosSystem {
        modules = [
          { nixpkgs.overlays = nixPins.overlays.linux.pre ++ nixPins.overlays.linux.post; }
          # …
        ];
      };
    };
}
```

With brew-nix, apply the lists around its overlay:
`overlays.darwin.pre ++ [ brew-nix.overlays.default ] ++ nixPins.autoPinOverlays ++ overlays.darwin.post`.

Every `overlays/*.nix` and `packages/*/default.nix` needs a manifest entry —
`check` reports drift in both directions.

## CLI

Run it from anywhere inside the flake (or set `NIX_PINS_REPO`):

```bash
nix run .#nix-pins                        # = check: dashboard + warnings
nix run .#nix-pins -- check --installed   # + which hosts actually install each entry (slow)
nix run .#nix-pins -- list                # entries + notes
nix run .#nix-pins -- update <name>       # bump version + hashes in place, verify build
nix run .#nix-pins -- update --all        # every enabled auto-updatable pin (no major bumps)
nix run .#nix-pins -- sync                # warnings → pick by number → run the batch
nix run .#nix-pins -- sync --select all   # the same, non-interactively
nix run .#nix-pins -- disable <name>      # flip `enable` (enable to undo)
```

A two-line wrapper script in your repo keeps this short; set `NIX_PINS_PROG` to
its path so hints print the right command.

Writing subcommands require the files nix-pins owns (manifest, overlays,
packages, Auto Pins, flake.nix/flake.lock) to be committed, so
`git checkout --` always undoes a run (`--allow-dirty` opts out). Nothing is
ever committed for you.

Exit codes: `0` clean, `1` work to do (outdated pin, drift), `2` the manifest or
the tool is broken — so CI can file an issue on `1` and fail on `2`.

## Auto Pins (brew-nix casks)

brew-nix freezes cask versions at the brew-api input's snapshot. `sync` can
outrun it for single casks by writing `<autoPinsDir>/<token>.json` — pure data
(token / version / url / hash / enabled); `auto-pins.nix` is the one place that
turns it into an overlay. The JSON file *is* the Entry: no manifest row.

Trust model: an Auto Pin trusts Homebrew. `url` + `sha256` come from
`formulae.brew.sh`, and `nix build` verifies the artifact against *that* hash,
not against an upstream signature. Casks with `sha256 = no_check` are never
pinned.

## Developing nix-pins

Point a consuming flake at a local checkout:

```bash
nix flake lock --override-input nix-pins path:/path/to/nix-pins
```

Requirements: `nix` (flakes enabled), `git`, network. `gh` is used for the
GitHub API when available (else `$GITHUB_TOKEN`, else anonymous).
