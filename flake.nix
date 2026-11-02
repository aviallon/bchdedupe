{
  description = "bchdedupe — hash-free deduplication for bcachefs";

  # Pinned, because the dev shell is what the git hooks and CI reproduce.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/86098a40ac2e2a727cde3dcbc59eeaa36ded8db3";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = eachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.python3Packages.buildPythonPackage {
            pname = "bchdedupe";
            version = "0.1.0";
            pyproject = true;
            src = self;
            build-system = [ pkgs.python3Packages.hatchling ];
            nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
            pythonImportsCheck = [ "bchdedupe" ];
            meta = {
              description = "Hash-free deduplication for bcachefs";
              license = pkgs.lib.licenses.mit;
              mainProgram = "bchdedupe";
            };
          };
        }
      );

      devShells = eachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShellNoCC {
            packages = [
              # One interpreter, so mypy can actually resolve pytest's stubs
              # instead of reporting it as a missing import.
              (pkgs.python3.withPackages (ps: [ ps.pytest ps.mypy ]))
              pkgs.ruff
              pkgs.fd
              pkgs.bcachefs-tools
            ];
            shellHook = ''
              # Import the working tree without installing it, so `python -c` and
              # ad-hoc scripts see the same code pytest does.
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              echo "bchdedupe dev shell — ruff $(ruff --version), $(mypy --version)"
              echo "hooks: git config core.hooksPath hooks"
            '';
          };
        }
      );
    };
}
