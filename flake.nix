{
  description = "DM-MC02 QEMU Linux build and test environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python311.withPackages (ps: [ ps.pip ps.setuptools ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        strictDeps = true;
        packages = with pkgs; [
          stdenv.cc
          bash
          git
          pkg-config
          ninja
          cmake
          gcc-arm-embedded
          ripgrep
          uv
          python
          coreutils
          findutils
          gnugrep
          gnused
          gawk
          diffutils
          procps
          util-linux
        ];
        buildInputs = with pkgs; [ glib zlib ];
        shellHook = ''
          export PYTHON=${python}/bin/python3
          export PKG_CONFIG=${pkgs.pkg-config}/bin/pkg-config
          export UV_PYTHON="$PYTHON"
          export UV_PYTHON_DOWNLOADS=never
          export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}
          export MUJOCO_GL="''${MUJOCO_GL:-disable}"
        '';
      };
    };
}
