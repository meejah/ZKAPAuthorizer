# Note: Passing arguments through here to customize the environment does not
# work on Nix 2.3.  It works with Nix 2.5.  I'm not sure about 2.4.
{ ... }@args:
let
  tests = import ./tests.nix args;
  inherit (tests) privatestorage lint-python;
  inherit (privatestorage) pkgs mach-nix tahoe-lafs zkapauthorizer;

  python-env = mach-nix.mkPython {
    inherit (zkapauthorizer.meta.mach-nix) python;
    providers = zkapauthorizer.meta.mach-nix.providers // {
      jedi = "wheel";
      parso = "wheel";
    };
    overridesPre = [
      (
        self: super: {
          inherit tahoe-lafs;
        }
      )
    ];
    requirements =
      ''
      pip
      jedi
      black
      isort
      flake8
      ${builtins.readFile ./docs/requirements.txt}
      ${builtins.readFile ./requirements/test.in}
      ${zkapauthorizer.requirements}
      '';
  };

in
pkgs.mkShell {
  # Avoid leaving .pyc all over the source tree when manually triggering tests
  # runs.
  PYTHONDONTWRITEBYTECODE = "1";

  buildInputs = [
    python-env
  ];
}
