{
  description = "BBC Most Read RSS server";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          bbc-rss = pkgs.callPackage ./nix/package.nix { };
          default = self.packages.${system}.bbc-rss;
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.bbc-rss}/bin/bbc-most-read-rss";
          meta.description = "Generate or serve the BBC Most Read RSS feed";
        };
      });

      checks = forAllSystems (system: {
        package = self.packages.${system}.bbc-rss;
      });

      nixosModules.bbc-rss = import ./nix/module.nix self;
      nixosModules.default = self.nixosModules.bbc-rss;
    };
}
