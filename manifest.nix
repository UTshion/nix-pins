# Manifest for scripts/nix-pins — every *hand-written* Entry (overlay / custom
# package / pinned flake input) that this repo Maintains on top of nixpkgs.
#
# Auto Pins are NOT listed here. Each `overlays/auto/<token>.json` is itself the
# Entry: `nix-pins.py` reads that directory directly, so the two can never drift
# apart. Adding or deleting an Auto Pin touches one file and nothing else.
#
# Domain vocabulary (see CONTEXT.md):
#   Entry   = one tracked unit (this list's elements)
#   Pin     = an Entry that fixes a version string; derived mechanically from the
#             presence of versionVar/versionRegex — there is no explicit flag
#   Purpose = why the Entry exists, which decides when it can be deleted:
#             "version-lag" — outpaces a lagging nixpkgs; droppable once nixpkgs
#                             catches up (drives the DROP? hint)
#             "compat"      — makes the package work at all (platform/build fix);
#                             NOT droppable on version parity (drives REVIEW /
#                             stillNeeded checks instead)
#             "packaging"   — we are the packager; no nixpkgs/brew equivalent
#
# Fields (null when not applicable):
#   name         unique id, used on the CLI (`nix-pins.py update <name>`)
#   enable       whether the Entry is wired. For overlays this IS the mechanism:
#                the flake applies exactly the enabled ones, so there are no
#                commented-out import lines to keep in sync. For
#                package/flake-input entries it is declared intent, read by the
#                dashboard and the exit code. Flip it with `nix-pins.py
#                disable/enable <name>` — never by editing the flake.
#   applyTo      (overlay) platforms whose package set receives the overlay;
#                default [ platform ]. direnv-cgo applies to both, for example.
#   afterCasks   (overlay) apply after brew-nix's brewCasks overlay —
#                required when the overlay wraps or overrides brewCasks.*.
#   purpose      list of purposes above (usually one; both is legal)
#   kind         "overlay" (overlays/*.nix) | "package" (packages/*/default.nix)
#                | "flake-input" (pinned in flake.nix inputs)
#                ("auto-pin" also exists, but is synthesized from
#                 overlays/auto/*.json and never written here)
#   file         repo-relative path holding the Entry (drift lint keys on this)
#   attr         attribute path inside pkgs once the overlay is applied (overlay kind)
#   platform     "darwin" | "linux" — selects the nixpkgs input to eval against
#                (darwin → nixpkgs-darwin/aarch64-darwin, linux → nixpkgs/x86_64-linux)
#   systems      systems whose hashes live in `file` (drives update; one eval per system)
#   source       where to ask for the latest upstream version:
#                "github:owner/repo" | "gitlab:owner/repo" | "pypi:name" | "npm:name"
#                | "brew-cask:token" | "cmd:<shell command printing a version>"
#                brew-cask/npm-style third-party feeds are Proxy Sources: safe for
#                update (a bogus version 404s at prefetch and reverts) but may lag.
#   tagPattern   optional regex; capture group 1 extracts the version from a git tag
#   nixpkgsAttr  attribute path in *plain* nixpkgs for the comparison column
#   versionVar   the `<var> = "x.y.z";` binding updated in `file` (default "version")
#   versionRegex optional regex overriding versionVar for *reading* the current pin
#   releaseJson  (flake-input) path inside the locked flake input named `name` whose
#                JSON has a `version` field (e.g. "release.json"). Makes the Entry a
#                Pin; `update` runs `nix flake lock --update-input <name>`.
#   fixedAgainst (compat) nixpkgs version of the package when the fix was written;
#                when nixpkgs moves past it, the dashboard asks for a re-verify (REVIEW)
#   stillNeeded  (compat, optional) a real Nix function `pkgs: bool`, called by
#                eval.nix against plain locked nixpkgs; true = overlay still
#                needed (FIX-OK), false = nixpkgs fixed it (FIXED-UPSTREAM →
#                delete the overlay). More precise than fixedAgainst; add one
#                when a crisp check exists.
#   update       false = check-only (auto-update not safe/possible for this Entry)
#   notes        free text, shown by `list` and in warnings
{
  entries = [
    # ─── Pins: flake inputs ─────────────────────────────────────────────────
    # NOTE: barik entry has been removed from this standalone manifest.
    # It was a personal fork reference (github:UTshion/barik). To pin a fork:
    # add a new entry below with your own fork URL.
    # {
    #   name = "barik";
    #   enable = true;
    #   purpose = [ "packaging" ];
    #   kind = "flake-input";
    #   file = "flake.nix";
    #   attr = null;
    #   platform = "darwin";
    #   systems = [ "aarch64-darwin" ];
    #   source = "github:YOUR_ACCOUNT/barik";
    #   tagPattern = null;
    #   nixpkgsAttr = null;
    #   versionVar = null;
    #   versionRegex = null;
    #   releaseJson = "release.json";
    #   fixedAgainst = null;
    #   update = true;
    #   notes = "Fork flake. Pinned version = locked input's release.json.";
    # }

    lanzaboote = {
      name = "lanzaboote";
      enable = true;
      purpose = [ "version-lag" ];
      kind = "flake-input";
      file = "flake.nix";
      attr = null;
      platform = "linux";
      systems = [ "x86_64-linux" ];
      source = "github:nix-community/lanzaboote";
      tagPattern = null;
      nixpkgsAttr = null;
      versionVar = null;
      versionRegex = "lanzaboote/v([0-9][0-9A-Za-z.]*)";
      fixedAgainst = null;
      update = false;
      notes = "Release pin. Bump by editing the input URL in flake.nix, then `nix flake lock --update-input lanzaboote`.";
    };

    # ─── Pins: custom packages (packages/*) ────────────────────────────────
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
      notes = "Replaces brewCasks.raycast (fetchurl name must keep the .dmg suffix). Consumed via callPackage in brew-nix.nix.";
    };
    {
      name = "voiceink-darwin";
      enable = true;
      purpose = [ "compat" ];
      kind = "package";
      file = "packages/voiceink-darwin/default.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:Beingpax/VoiceInk";
      tagPattern = null;
      nixpkgsAttr = null;
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "Replaces the brew cask: its DMG needs 7zz (undmg can't mount it). Consumed via callPackage in brew-nix.nix.";
    };

    # ─── Pins: overlays ─────────────────────────────────────────────────────
    {
      name = "postman";
      applyTo = [ "darwin" "linux" ];
      purpose = [ "version-lag" ];
      kind = "overlay";
      file = "overlays/postman.nix";
      attr = "postman";
      platform = "darwin";
      systems = [
        "aarch64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      source = "brew-cask:postman";
      tagPattern = null;
      nixpkgsAttr = "postman";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "Three hashes (osx_arm64 zip + both Linux tarballs). Proxy Source: mac cask version stands in for all artifacts.";
    };
    {
      name = "claude-code";
      applyTo = [ "darwin" "linux" ];
      purpose = [ "version-lag" ];
      kind = "overlay";
      file = "overlays/claude-code.nix";
      attr = "claude-code";
      platform = "darwin";
      systems = [
        "aarch64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      source = "npm:@anthropic-ai/claude-code";
      tagPattern = null;
      nixpkgsAttr = "claude-code";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "Overrides the `manifest` arg of nixpkgs' claude-code (three checksums). Upstream ships a patch release most days, so DROP? will rarely fire — keep the pin.";
    };
    {
      name = "marimo";
      enable = false;
      purpose = [ "version-lag" ];
      kind = "overlay";
      file = "overlays/marimo.nix";
      attr = "marimo";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "pypi:marimo";
      tagPattern = null;
      nixpkgsAttr = "marimo";
      versionVar = "marimoVersion";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "If a bump raises the msgspec requirement, update msgspecVersion/Hash by hand (see msgspec entry).";
    };
    {
      name = "msgspec";
      enable = false;
      purpose = [ "version-lag" ];
      kind = "overlay";
      file = "overlays/marimo.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "pypi:msgspec";
      tagPattern = null;
      nixpkgsAttr = "python313Packages.msgspec";
      versionVar = "msgspecVersion";
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "Companion pin inside marimo.nix; only bump when marimo requires it (not directly evaluable).";
    };
    {
      name = "burpsuite-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/burpsuite-darwin.nix";
      attr = "burpsuite";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "brew-cask:burp-suite";
      tagPattern = null;
      nixpkgsAttr = "burpsuite";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "nixpkgs burpsuite uses buildFHSEnv (Linux-only); this repackages the JAR for Darwin.";
    };
    {
      name = "caido-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/caido-darwin.nix";
      attr = "caido";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:caido/caido";
      tagPattern = null;
      nixpkgsAttr = "caido";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "Two inner srcs (cli + desktop) not reachable via drv.src — update both hashes by hand.";
    };
    {
      name = "eyewitness-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/eyewitness-darwin.nix";
      attr = "eyewitness";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:redsiege/EyeWitness";
      tagPattern = null;
      nixpkgsAttr = "eyewitness";
      versionVar = "version";
      versionRegex = null;
      fixedAgainst = null;
      update = true;
      notes = "Repackaged without xvfb-run for Darwin; pins a version but exists for compat.";
    };

    # ─── Compat overlays: no version pinned; watch nixpkgs and drop when fixed ──
    {
      name = "obsidian-darwin";
      enable = false;
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
      notes = "Remove once nixpkgs-unstable ships the fix for obsidian on darwin.";
    };
    {
      name = "highlight-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/highlight-darwin.nix";
      attr = "highlight";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "gitlab:saalen/highlight";
      tagPattern = null;
      nixpkgsAttr = "highlight";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = "4.20";
      update = false;
      notes = "FIXED-UPSTREAM: nixpkgs 4.21 no longer carries the stale patch; plain build OK. Disabled — delete file when convenient.";
    };
    {
      name = "wrangler-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/wrangler-darwin.nix";
      attr = "wrangler";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "npm:wrangler";
      tagPattern = null;
      nixpkgsAttr = "wrangler";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = "4.93.0";
      update = false;
      notes = "FIXED-UPSTREAM: nixpkgs wrangler builds on the default nodejs. Disabled — delete file when convenient.";
    };
    {
      name = "wapiti-darwin";
      enable = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/wapiti-darwin.nix";
      attr = "wapiti";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "pypi:wapiti3";
      tagPattern = null;
      nixpkgsAttr = "wapiti";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = "3.3.2";
      update = false;
      notes = "Disables one PHP-version-dependent test. Re-verified against 3.3.2: plain build still fails without this.";
    };
    {
      name = "wfuzz-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/wfuzz-darwin.nix";
      attr = "wfuzz";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "pypi:wfuzz";
      tagPattern = null;
      nixpkgsAttr = "wfuzz";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = "3.1.1";
      update = false;
      notes = "FIXED-UPSTREAM: nixpkgs ships its own Drop-pkg_resources patch. Disabled — delete file when convenient.";
    };
    {
      name = "jeepney-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/jeepney-darwin.nix";
      attr = "python3Packages.jeepney";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "pypi:jeepney";
      tagPattern = null;
      nixpkgsAttr = "python3Packages.jeepney";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "FIXED-UPSTREAM: jeepney 0.9 builds clean on default python. Disabled — delete file when convenient.";
    };
    {
      name = "steghide-darwin";
      enable = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/steghide-darwin.nix";
      attr = "steghide";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = null;
      tagPattern = null;
      nixpkgsAttr = "steghide";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = "0.5.1.1";
      update = false;
      notes = "macOS / clang build fixes; rewritten for nixpkgs 0.5.1.1.";
    };
    {
      name = "curl-impersonate-darwin";
      enable = false;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/curl-impersonate-darwin.nix";
      attr = "curl-impersonate";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:lexiforest/curl-impersonate";
      tagPattern = null;
      nixpkgsAttr = "curl-impersonate";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "FIXED-UPSTREAM: plain curl-impersonate builds on Darwin. Disabled — delete file when convenient.";
    };
    {
      name = "ghidra-darwin";
      enable = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/ghidra-darwin.nix";
      attr = "ghidra";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:NationalSecurityAgency/ghidra";
      tagPattern = "Ghidra_([0-9][0-9.]*)_build";
      nixpkgsAttr = "ghidra";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "Launcher fixes for copyApps; tied to a specific nixpkgs PR.";
    };
    {
      name = "direnv-cgo";
      applyTo = [ "darwin" "linux" ];
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/direnv-cgo.nix";
      attr = "direnv";
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "github:direnv/direnv";
      tagPattern = null;
      nixpkgsAttr = "direnv";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "CGO_ENABLED=1 (applies on both platforms).";
    };
    {
      name = "claude-darwin";
      enable = false;
      afterCasks = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/claude-darwin.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "brew-cask:claude";
      tagPattern = null;
      nixpkgsAttr = null;
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "symlinkJoin wrapper over brewCasks.claude (shim removal + dangling locale symlinks).";
    };
    {
      name = "maltego";
      enable = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/maltego.nix";
      attr = "maltego";
      platform = "linux";
      systems = [ "x86_64-linux" ];
      source = null;
      tagPattern = null;
      nixpkgsAttr = "maltego";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "JAVA_HOME wrapper.";
    };
    {
      name = "swt_tempfix";
      enable = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/swt_tempfix.nix";
      attr = "swt";
      platform = "linux";
      systems = [ "x86_64-linux" ];
      source = null;
      tagPattern = null;
      nixpkgsAttr = "swt";
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "-Wno-error=deprecated-declarations.";
    };
    {
      name = "wireshark-darwin";
      enable = false;
      afterCasks = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/wireshark-darwin.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "brew-cask:wireshark-app";
      tagPattern = null;
      nixpkgsAttr = null;
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "brewCasks src filename fix (hardcodes Wireshark-4.6.2.dmg — revisit on cask bumps).";
    };
    {
      name = "ilok-license-manager-darwin";
      enable = false;
      afterCasks = true;
      purpose = [ "compat" ];
      kind = "overlay";
      file = "overlays/ilok-license-manager-darwin.nix";
      attr = null;
      platform = "darwin";
      systems = [ "aarch64-darwin" ];
      source = "brew-cask:ilok-license-manager";
      tagPattern = null;
      nixpkgsAttr = null;
      versionVar = null;
      versionRegex = null;
      fixedAgainst = null;
      update = false;
      notes = "brewCasks hash + unpack fix.";
    };
  ];
}
