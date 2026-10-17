{
  description = "nix-pins — dashboard + updater for the overlays, packages and pins a flake maintains on top of nixpkgs";

  # Only used to build the CLI wrapper (python3). The evaluation half takes the
  # consumer's own `lib` and nixpkgs inputs, so it never evaluates against ours.
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      lib = {
        # The evaluation half; expose the result as your flake's `nix-pins`
        # output — the CLI reads everything through `.#nix-pins.*`.
        # Arguments are documented at the top of eval.nix.
        mkNixPins = import ./eval.nix;

        # Auto Pins (`<dir>/<token>.json`) → overlays, applied after brew-nix.
        # mkNixPins exposes the same list as `autoPinOverlays` when given
        # `autoPinsDir`.
        autoPinOverlays = import ./auto-pins.nix;
      };

      packages = forAllSystems (pkgs: {
        default = pkgs.callPackage ./package.nix { };
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
      });
    };
}
