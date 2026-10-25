{ lib, python3Packages, cacert, git, bash }:

let
  project = builtins.fromTOML (builtins.readFile ../pyproject.toml);
in
python3Packages.buildPythonApplication {
  pname = project.project.name;
  version = project.project.version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../bbc_rss.py
      ../bbc_server.py
      ../tests
      ../scripts/pages-history.sh
    ];
  };

  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.beautifulsoup4 ];
  nativeCheckInputs = [ git bash ];

  # urllib needs a CA bundle both in systemd and when run outside NixOS.
  makeWrapperArgs = [ "--set-default" "SSL_CERT_FILE" "${cacert}/etc/ssl/certs/ca-bundle.crt" ];

  checkPhase = ''
    runHook preCheck
    export HOME="$TMPDIR/test-home"
    mkdir -p "$HOME"
    python -m unittest discover -s tests -v
    runHook postCheck
  '';
  pythonImportsCheck = [ "bbc_rss" "bbc_server" ];

  meta = {
    description = project.project.description;
    mainProgram = "bbc-most-read-rss";
    platforms = lib.platforms.unix;
  };
}
