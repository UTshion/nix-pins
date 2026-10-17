# eval.nix — the evaluation half of nix-pins: `lib.mkNixPins { … }`.
#
# The consuming flake exposes the result as its `nix-pins` output. Everything the
# CLI needs *from evaluation* is a flake attribute, so the CLI never composes Nix
# expressions in strings — pure evaluation throughout:
#
#   .#nix-pins.manifest                     { platforms, entries } — JSON-safe
#   .#nix-pins.layout                       repo-relative paths the CLI reads/edits
#   .#nix-pins.report                       nixpkgs version + stillNeeded verdict per entry
#   .#nix-pins.src."<name>"."<system>"      the entry's src, described (urls/hash/mode/name/rev)
#   .#nix-pins.drv."<name>"."<system>"      the entry's derivation, for `nix build`
#   .#nix-pins.casks."<token>"              a cask as the host resolves it (Auto Pin build check)
#   .#nix-pins.overlays.<platform>.{pre,post}  the overlay lists the flake applies,
#                                           built from each entry's `enable` field
#   .#nix-pins.autoPinOverlays              the Auto Pins in autoPinsDir, as overlays
#   .#nix-pins.applied.<platform>           names behind those lists (dashboard truth)
#   .#nix-pins.installed."<host>"."<name>"  is the entry's attr in that host's packages?
#   .#nix-pins.brewApiPath                  store path of the brew-api input
#
# `stillNeeded` in the manifest is a real Nix function (`pkgs: bool`); only its
# verdict crosses to the CLI. `enable` is the wiring mechanism itself.
{
  lib,
  root, # flake source root (./.)
  manifest, # path to the manifest (inside root)
  platforms, # { <platform> = { input = <nixpkgs flake input>; defaultSystem = "…"; }; }
  brewApi ? null, # the brew-api flake input, when the repo uses brew-nix
  brewApiInput ? "brew-api", # its name in flake.nix (`nix flake lock --update-input`)
  hosts ? { }, # { <name> = darwinConfigurations.X / nixosConfigurations.Y; } for `installed`
  autoPinsDir ? null, # directory of Auto Pin JSON files (inside root)
  caskModule ? null, # file whose uncommented `brewCasks.<token>` lines are the wired casks
  caskPkgs ? null, # package set casks resolve in (Auto Pins applied), e.g. a host's `pkgs`
  overlaysDir ? root + "/overlays", # drift lint: every *.nix here needs an entry
  packagesDir ? root + "/packages", # drift lint: every */default.nix here needs an entry
  config ? {
    allowUnfree = true;
  },
}:
let
  inherit (builtins)
    attrValues
    concatMap
    dirOf
    elem
    filter
    hasAttr
    listToAttrs
    mapAttrs
    tryEval
    ;

  knownPlatforms = builtins.attrNames platforms;

  checkEntry =
    e:
    if !(hasAttr (e.platform or "") platforms) then
      throw "nix-pins: entry ${e.name or "?"} has unknown platform `${toString (e.platform or null)}` (known: ${toString knownPlatforms})"
    else if e.kind != "auto-pin" && !(e ? enable) then
      throw "nix-pins: entry ${e.name} has no `enable` field — wiring is data now, add one"
    else
      e;

  entries = map checkEntry (import manifest).entries;

  byName = listToAttrs (
    map (e: {
      inherit (e) name;
      value = e;
    }) entries
  );

  # The CLI edits the working tree, not the store copy, so it gets paths
  # relative to the flake root.
  relPath =
    p:
    let
      r = toString root;
      s = toString p;
    in
    if lib.hasPrefix "${r}/" s then
      lib.removePrefix "${r}/" s
    else
      throw "nix-pins: ${s} is not inside the flake root ${r}";

  optionalRel = p: if p == null then null else relPath p;

  systemsFor =
    e: if (e.systems or null) != null then e.systems else [ platforms.${e.platform}.defaultSystem ];

  pkgsFor =
    {
      platform,
      system,
      overlays ? [ ],
    }:
    import platforms.${platform}.input { inherit system overlays config; };

  # One plain (overlay-free) package set per platform, shared by every lookup.
  plainPkgs = lib.genAttrs knownPlatforms (
    p:
    pkgsFor {
      platform = p;
      system = platforms.${p}.defaultSystem;
    }
  );

  versionIn =
    pkgs: attrPath:
    let
      r = tryEval (lib.attrByPath (lib.splitString "." attrPath) null pkgs);
      v = if r.success then r.value else null;
      t = tryEval (if v == null then null else (v.version or null));
    in
    if t.success then t.value else null;

  # JSON-safe view of an entry: the function field is replaced by the fact of
  # its presence, and `systems` is always resolved.
  clean =
    e:
    (removeAttrs e [ "stillNeeded" ])
    // {
      systems = systemsFor e;
      hasStillNeeded = (e.stillNeeded or null) != null;
    };

  reportFor = e: {
    nixpkgs =
      if (e.nixpkgsAttr or null) != null then versionIn plainPkgs.${e.platform} e.nixpkgsAttr else null;
    # true = still needed, false = fixed upstream, null = no predicate,
    # "error" = the predicate itself failed to evaluate.
    stillNeeded =
      if (e.stillNeeded or null) == null then
        null
      else
        let
          r = tryEval (e.stillNeeded plainPkgs.${e.platform});
        in
        if r.success then r.value else "error";
  };

  overlayOf = e: import (root + "/${e.file}");

  drvFor =
    e: system:
    if e.kind == "overlay" then
      let
        pkgs = pkgsFor {
          inherit (e) platform;
          inherit system;
          overlays = [ (overlayOf e) ];
        };
      in
      lib.attrByPath (lib.splitString "." e.attr)
        (throw "nix-pins: attr ${e.attr} not found after applying ${e.file}")
        pkgs
    else if e.kind == "package" then
      (pkgsFor {
        inherit (e) platform;
        inherit system;
      }).callPackage
        (root + "/${dirOf e.file}")
        { }
    else
      throw "nix-pins: entry ${e.name} (kind ${e.kind}) is not evaluable as a derivation";

  srcFor =
    e: system:
    let
      src = (drvFor e system).src;
    in
    {
      urls = (if src ? urls then src.urls else [ ]) ++ (lib.optional (src ? url) src.url);
      hash = src.outputHash or null;
      mode = src.outputHashMode or "flat";
      name = src.name or null;
      rev = src.rev or null;
    };

  evaluable = e: (e.kind == "overlay" && (e.attr or null) != null) || e.kind == "package";

  perSystem =
    f:
    listToAttrs (
      map (e: {
        inherit (e) name;
        value = lib.genAttrs (systemsFor e) (system: f e system);
      }) (filter evaluable entries)
    );

  # ── wiring: enable → overlay lists ────────────────────────────────────────
  # `pre` is applied before platform-specific base overlays (brew-nix on darwin);
  # `post` after — an overlay that wraps/overrides brewCasks.* sets
  # `afterCasks = true`. Two entries may share one file (companion pins, e.g.
  # marimo/msgspec); the file is imported once.
  overlayFiles =
    platform: post:
    lib.unique (
      map (e: e.file) (
        filter (
          e:
          e.kind == "overlay"
          && e.enable
          && elem platform (e.applyTo or [ e.platform ])
          && (e.afterCasks or false) == post
        ) entries
      )
    );

  overlaysFor = platform: post: map (f: import (root + "/${f}")) (overlayFiles platform post);

  appliedFor =
    platform:
    map (e: e.name) (
      filter (e: e.kind == "overlay" && e.enable && elem platform (e.applyTo or [ e.platform ])) entries
    );

  # ── installed: is the entry's attr in a host's package closure? ───────────
  # Answered against the *host's own* pkgs (its overlays applied), by drvPath
  # identity over environment.systemPackages plus every HM user's home.packages.
  # Expensive (instantiates the host's package list); only forced on demand.
  hostPackages =
    host:
    let
      sys = if host.config ? environment then (host.config.environment.systemPackages or [ ]) else [ ];
      hmUsers =
        if host.config ? home-manager then attrValues (host.config.home-manager.users or { }) else [ ];
      hm = concatMap (u: u.home.packages or [ ]) hmUsers;
      drvPathOf =
        p:
        let
          r = tryEval (p.drvPath or null);
        in
        if r.success then r.value else null;
    in
    filter (p: p != null) (map drvPathOf (sys ++ hm));

  # Auto Pins (JSON data) take part in `installed` too: a DROP?-ready pin that is
  # not even installed anywhere is simply deletable. Their target is the cask
  # attr as the host resolves it — pinned when enabled, base when not.
  autoPinEntries =
    if autoPinsDir == null then
      [ ]
    else
      map
        (
          n:
          let
            d = builtins.fromJSON (builtins.readFile (autoPinsDir + "/${n}"));
          in
          {
            name = d.token;
            kind = "overlay";
            attr = "brewCasks.${d.token}";
          }
        )
        (
          filter (n: builtins.match ".*\\.json" n != null) (builtins.attrNames (builtins.readDir autoPinsDir))
        );

  targetIn =
    pkgs: e:
    if e.kind == "package" then
      pkgs.callPackage (root + "/${dirOf e.file}") { }
    else if e.kind == "overlay" && (e.attr or null) != null then
      lib.attrByPath (lib.splitString "." e.attr) null pkgs
    else
      null;

  installedIn =
    host: e:
    let
      target = targetIn host.pkgs e;
      r = tryEval (if target == null then null else elem target.drvPath (hostPackages host));
    in
    if r.success then r.value else null;
in
{
  manifest = {
    platforms = knownPlatforms;
    entries = map clean entries;
  };

  # Where things live, so the CLI hardcodes no repo layout. null = feature not
  # used by this repo (no Auto Pins / no brew-nix casks).
  layout = {
    manifest = relPath manifest;
    autoPins = optionalRel autoPinsDir;
    caskModule = optionalRel caskModule;
    overlays = relPath overlaysDir;
    packages = relPath packagesDir;
    brewApiInput = if brewApi == null then null else brewApiInput;
  };

  report = listToAttrs (
    map (e: {
      inherit (e) name;
      value = reportFor e;
    }) entries
  );

  src = perSystem srcFor;
  drv = perSystem drvFor;

  casks = if caskPkgs == null then { } else caskPkgs.brewCasks or { };

  overlays = lib.genAttrs knownPlatforms (p: {
    pre = overlaysFor p false;
    post = overlaysFor p true;
  });
  applied = lib.genAttrs knownPlatforms appliedFor;

  autoPinOverlays = if autoPinsDir == null then [ ] else import ./auto-pins.nix autoPinsDir;

  installed = mapAttrs (
    _: host:
    listToAttrs (
      map (e: {
        inherit (e) name;
        value = installedIn host e;
      }) (entries ++ autoPinEntries)
    )
  ) hosts;

  brewApiPath = if brewApi == null then null else brewApi.outPath;

  # exposed for tests / debugging
  inherit byName;
}
