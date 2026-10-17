# nix-pins Auto Pin loader: `lib.autoPinOverlays <dir>` → list of overlays.
#
# Auto Pins are *data*, not code: each `<token>.json` in `dir` holds one pin
# (token / version / url / hash / enabled) and this file is the only place that
# knows how to turn a pin into an overlay. Regenerating a pin therefore never
# rewrites the override mechanism — changing that strategy is a one-file edit
# here instead of a re-generation of every pin. It ships with the CLI that
# writes the JSON, so the two always agree on `schema`.
#
# A JSON file *is* the entry: there is no manifest row to keep in sync with it.
#
#   generate / refresh   nix-pins sync   (or update <token>)
#   disable              nix-pins disable <token>
#                        (flips "enabled" in the JSON; the file is kept)
#   delete               once `check` reports DROP? (the brew-api input caught
#                        up), delete the JSON file — nothing else to clean up
#
# Apply the result after brew-nix's overlay (it overrides `brewCasks.*`).
#
# NOTE: a git+file flake only sees *tracked* files, so a new pin has to be
# `git add`-ed before darwin-rebuild can see it (nix-pins does that for you).
dir:
let
  # Bumped when the JSON layout changes; pins carrying another schema refuse to
  # load rather than being silently misread. Must match AUTO_SCHEMA in nix-pins.py.
  schema = 1;

  isPin = name: builtins.match ".*\\.json" name != null;

  readPin =
    name:
    let
      pin = builtins.fromJSON (builtins.readFile (dir + "/${name}"));
      need =
        field:
        if builtins.hasAttr field pin then
          builtins.getAttr field pin
        else
          throw "nix-pins: Auto Pin ${name} is missing the required field `${field}`";
      found = pin.schema or 0;
    in
    if found != schema then
      throw (
        "nix-pins: Auto Pin ${name} declares schema ${toString found}, but this "
        + "loader understands ${toString schema} — regenerate it with `nix-pins update`"
      )
    else
      let
        entry = {
          inherit name;
          kind = need "kind";
          token = need "token";
          version = need "version";
          url = need "url";
          hash = need "hash";
          enabled = need "enabled";
        };
      in
      # Force every field now, so a malformed pin is reported when the pin is
      # read rather than much later, from inside a fetcher.
      builtins.deepSeq (builtins.attrValues entry) entry;

  # data → overlay. The single definition of the override mechanism.
  #
  # The attribute *names* an overlay contributes must never depend on `prev` or
  # `final`: deciding them would force the final package set, which needs this
  # overlay — infinite recursion. So both guards below live inside attribute
  # *values*, which stay lazy, instead of wrapping the returned attrset.
  mkPin =
    pin:
    if pin.kind == "brew-cask" then
      final: prev: {
        brewCasks =
          (
            if prev ? brewCasks then
              prev.brewCasks
            else
              throw (
                "nix-pins: ${pin.name} pins brewCasks.${pin.token} but `brewCasks` is absent — "
                + "the Auto Pin loader must be applied after brew-nix.overlays.default"
              )
          )
          // {
            ${pin.token} =
              if builtins.hasAttr pin.token prev.brewCasks then
                (builtins.getAttr pin.token prev.brewCasks).overrideAttrs (_: {
                  inherit (pin) version;
                  src = final.fetchurl { inherit (pin) url hash; };
                })
              else
                throw (
                  "nix-pins: ${pin.name} pins brewCasks.${pin.token}, which the brew-api input "
                  + "does not provide — delete the pin or update the input"
                );
          };
      }
    else
      throw "nix-pins: ${pin.name} has unknown kind `${pin.kind}` (known: brew-cask)";

  pins = map readPin (builtins.filter isPin (builtins.attrNames (builtins.readDir dir)));
in
# Disabled pins contribute no overlay at all, rather than an empty one.
map mkPin (builtins.filter (pin: pin.enabled) pins)
