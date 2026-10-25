self:
{ config, lib, pkgs, utils, ... }:

let
  cfg = config.services.bbc-rss;
in
{
  options.services.bbc-rss = {
    enable = lib.mkEnableOption "the BBC Most Read RSS server";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.bbc-rss;
      defaultText = lib.literalExpression "bbc-rss.packages.\${pkgs.stdenv.hostPlatform.system}.bbc-rss";
      description = "BBC Most Read RSS package to run.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "HTTP bind address. Use a reverse proxy for public HTTPS access.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8000;
      description = "HTTP listen port.";
    };

    interval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 900;
      description = "Seconds between refresh attempts.";
    };

    feedUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "https://rss.example.org/feed.xml";
      description = "Public feed URL advertised in the Atom self link.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.bbc-rss = {
      description = "BBC Most Read RSS server and updater";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      serviceConfig = {
        Type = "simple";
        DynamicUser = true;
        StateDirectory = "bbc-rss";
        StateDirectoryMode = "0750";
        WorkingDirectory = "/var/lib/bbc-rss";
        ExecStart = utils.escapeSystemdExecArgs ([
          (lib.getExe cfg.package)
          "--serve"
          "--host" cfg.host
          "--port" (toString cfg.port)
          "--interval" (toString cfg.interval)
          "--output" "/var/lib/bbc-rss/feed.xml"
        ] ++ lib.optionals (cfg.feedUrl != null) [ "--feed-url" cfg.feedUrl ]);
        Restart = "on-failure";
        RestartSec = 10;
        TimeoutStopSec = 10;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
      };
    };
  };
}
