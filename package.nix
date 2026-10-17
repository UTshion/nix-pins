# The CLI: stdlib-only Python, wrapped with a pinned interpreter. `nix`, `git`
# and (optionally) `gh` are taken from PATH — the user's own Nix, not a copy.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
}:
stdenvNoCC.mkDerivation {
  pname = "nix-pins";
  version = "0.2.0";

  src = ./nix-pins.py;
  dontUnpack = true;

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    install -Dm644 $src $out/libexec/nix-pins/nix-pins.py
    # Unbuffered, so progress of long runs shows up promptly in `| tee` / CI logs.
    makeWrapper ${lib.getExe python3} $out/bin/nix-pins \
      --add-flags $out/libexec/nix-pins/nix-pins.py \
      --set-default PYTHONUNBUFFERED 1
    runHook postInstall
  '';

  meta = {
    description = "Dashboard + updater for the overlays, packages and pins a Nix flake maintains on top of nixpkgs";
    homepage = "https://github.com/UTshion/nix-pins";
    mainProgram = "nix-pins";
    platforms = lib.platforms.unix;
  };
}
