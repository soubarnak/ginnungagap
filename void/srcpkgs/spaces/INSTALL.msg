Spaces is installed. Check the setup with: sudo spaces-void doctor

  sudo spaces create ubuntu --preset basic   (also arch, fedora, kali)
  spaces enter ubuntu

New spaces get a user namespace (root in them is not host root); spaces made before 0.0.2 keep host root until:
  sudo spaces-void userns enable NAME   (while it is stopped; --all for every space)
Opt out for one new space: spaces create --no-userns TYPE

A new Kali space installs no Kali tool set; add tools with apt (or pick one in the TUI).

Autostart of spaces at login (optional, off by default):
  sudo ln -s /etc/sv/spaces-autostart /var/service/

Desktop (niri): publish the session environment at login, add to config.kdl:
  spawn-at-startup "spaces-session-env" "publish"

Opt-in shell functions (ubuntu, arch, ...): source in your shell rc
  /usr/share/spaces/void/shell/spaces.sh
