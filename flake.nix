{
  description = "Version pin tracking tooling — evaluation half.";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    nixpkgs-darwin.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      nixpkgs-darwin,
      ...
    }:
    let
      nixPins = import ./eval.nix {
        lib = nixpkgs.lib;
        root = ./.;
        manifest = ./manifest.nix;
        platforms = {
          linux = {
            input = nixpkgs;
            defaultSystem = "x86_64-linux";
          };
          darwin = {
            input = nixpkgs-darwin;
            defaultSystem = "aarch64-darwin";
          };
        };
        brewApi = null;
        hosts = { };
      };
    in
    {
      # Evaluation outputs (same surface as the dotnix integration):
      #   nix-pins.manifest       { platforms, entries }
      #   nix-pins.report         version + stillNeeded per entry
      #   nix-pins.src.<name>.<system>  entry src description (urls/hash/…)
      #   nix-pins.drv.<name>.<system>  entry derivation
      #   nix-pins.overlays.<platform>  pre / post overlay lists
      #   nix-pins.applied.<platform>   enabled overlay names
      #   nix-pins.installed        (empty when hosts = { })
      nix-pins = nixPins;
    };
}
