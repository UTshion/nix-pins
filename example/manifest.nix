# Example nix-pins manifest — every *hand-written* Entry (overlay / custom
# package / pinned flake input) a repo maintains on top of nixpkgs.
#
# Auto Pins are NOT listed here. Each `<autoPinsDir>/<token>.json` is itself the
# Entry: the CLI reads that directory directly, so the two can never drift
# apart. Adding or deleting an Auto Pin touches one file and nothing else.
#
# Vocabulary:
#   Entry   = one tracked unit (this list's elements)
#   Pin     = an Entry that fixes a version string; derived mechanically from the
#             presence of versionVar/versionRegex/releaseJson — no explicit flag
#   Purpose = why the Entry exists, which decides when it can be deleted:
#             "version-lag" — outpaces a lagging nixpkgs; droppable once nixpkgs
#                             catches up (drives the DROP? hint)
#             "compat"      — makes the package work at all (platform/build fix);
#                             NOT droppable on version parity (drives REVIEW /
#                             stillNeeded checks instead)
#             "packaging"   — we are the packager; no nixpkgs/brew equivalent
#
# Fields (null when not applicable):
#   name         unique id, used on the CLI (`nix-pins update <name>`)
#   enable       whether the Entry is wired. For overlays this IS the mechanism:
#                the flake applies exactly the enabled ones (nixPins.overlays),
#                so there are no commented-out import lines to keep in sync. For
#                package/flake-input entries it is declared intent, read by the
#                dashboard and the exit code. Flip it with `nix-pins
#                disable/enable <name>` — never by editing flake.nix.
#   applyTo      (overlay) platforms whose package set receives the overlay;
#                default [ platform ].
#   afterCasks   (overlay) apply after brew-nix's brewCasks overlay (lands in
#                overlays.<platform>.post) — required when the overlay wraps or
#                overrides brewCasks.*.
#   purpose      list of purposes above (usually one; both is legal)
#   kind         "overlay" (overlays/*.nix) | "package" (packages/*/default.nix)
#                | "flake-input" (pinned in flake.nix inputs)
#                ("auto-pin" also exists, but is synthesized from the Auto Pin
#                 JSON files and never written here)
#   file         repo-relative path holding the Entry (drift lint keys on this)
#   attr         attribute path inside pkgs once the overlay is applied (overlay kind)
#   platform     a key of mkNixPins' `platforms` — selects the nixpkgs input to
#                eval against (and its defaultSystem)
#   systems      systems whose hashes live in `file` (drives update; one eval per system)
#   source       where to ask for the latest upstream version:
#                "github:owner/repo" | "gitlab:owner/repo" | "pypi:name" | "npm:name"
#                | "brew-cask:token" | "cmd:<shell command printing a version>"
#                brew-cask/npm-style third-party feeds are Proxy Sources: safe for
#                update (a bogus version 404s at prefetch and reverts) but may lag.
#                `cmd:` runs a shell command and needs --allow-cmd-sources.
#   tagPattern   optional regex; capture group 1 extracts the version from a git tag
#   nixpkgsAttr  attribute path in *plain* nixpkgs for the comparison column
#   versionVar   the `<var> = "x.y.z";` binding updated in `file` (default "version")
#   versionRegex optional regex overriding versionVar for *reading* the current pin
#   releaseJson  (flake-input) path inside the locked flake input named `name` whose
#                JSON has a `version` field (e.g. "release.json"). Makes the Entry a
#                Pin; `update` runs `nix flake lock --update-input <name>` and
#                verifies with `nix build .#<name>`.
#   fixedAgainst (compat) nixpkgs version of the package when the fix was written;
#                when nixpkgs moves past it, the dashboard asks for a re-verify (REVIEW)
#   stillNeeded  (compat, optional) a real Nix function `pkgs: bool`, called
#                against plain locked nixpkgs; true = overlay still needed
#                (FIX-OK), false = nixpkgs fixed it (FIXED-UPSTREAM → delete the
#                overlay). More precise than fixedAgainst; add one when a crisp
#                check exists.
#   update       false = check-only (auto-update not safe/possible for this Entry)
#   notes        free text, shown by `list` and in warnings
{
  entries = [
    # A version-lag pin: an overlay that bumps version + hashes ahead of
    # nixpkgs, for both package sets. `update` rewrites `version = "…"` and every
    # system's hash, then build-verifies on the current system.
    {
      name = "postman";
      enable = true;
      applyTo = [
        "darwin"
        "linux"
      ];
      purpose = [ "version-lag" ];
      kind = "overlay";
      file = "overlays/postman.nix";
      attr = "postman";
      platform = "darwin";
      systems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];
      source = "brew-cask:postman";
      tagPattern = null;
      nixpkgsAttr = "postman";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "Proxy Source: the mac cask version stands in for every artifact.";
    }

    # A compat fix with a predicate: FIXED-UPSTREAM as soon as nixpkgs no
    # longer hardcodes sourceRoot — even without a version bump.
    {
      name = "obsidian-darwin";
      enable = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/obsidian-darwin.nix";
      attr = "obsidian";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:obsidianmd/obsidian-releases";
      tagPattern = null;
      nixpkgsAttr = "obsidian";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = "1.13.4";
      stillNeeded = pkgs: (pkgs.obsidian.sourceRoot or null) != null;
      update = false;
      notes = "Remove once nixpkgs ships the darwin sourceRoot fix.";
    }

    # A package we maintain ourselves (callPackage'd from packages/<name>/).
    {
      name = "raycast-darwin";
      enable = true;
      purpose = [ "compat" ];
      kind = "package";
      file = "packages/raycast-darwin/default.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "brew-cask:raycast";
      tagPattern = null;
      nixpkgsAttr = "raycast";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "Replaces brewCasks.raycast (its DMG needs 7zz).";
    }

    # A flake input whose version lives in the input's own release.json.
    {
      name = "my-app";
      enable = true;
      purpose = [ "packaging" ];
      kind = "flake-input";
      file = "flake.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:YOU/my-app";
      tagPattern = null;
      nixpkgsAttr = null;
      versionVar = null;
      versionRegex = null;
      releaseJson = "release.json";
      fixedAgainst = null;
      update = true;
      notes = "Bump: tag upstream (CI rewrites release.json), then `nix-pins update my-app`.";
    }
  ];
}
