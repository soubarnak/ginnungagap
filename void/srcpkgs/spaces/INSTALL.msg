Spaces is installed. Check the setup with: sudo spaces-void doctor

  sudo spaces create ubuntu --preset basic   (also arch, fedora, kali)
  spaces enter ubuntu

Autostart of spaces at login (optional, off by default):
  sudo ln -s /etc/sv/spaces-autostart /var/service/

Desktop (niri): publish the session environment at login, add to config.kdl:
  spawn-at-startup "spaces-session-env" "publish"

Opt-in shell functions (ubuntu, arch, ...): source in your shell rc
  /usr/share/spaces/void/shell/spaces.sh
