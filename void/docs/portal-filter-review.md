# Review of the host portal proxy filter (M5, documentation only)

Source: `src/spaces/session.py`, `_portal_policy_arguments` and `HOST_PORTAL_INTERFACES`
(upstream, unchanged in this fork). The guest talks to a filtered session bus
(`xdg-dbus-proxy --filter`, run as the user, socket bound at `/run/user/UID/bus`). Everything
below is read from the filter rules; only OpenURI, notifications and the Registry were exercised
at runtime (M4). No code was changed.

What the filter lets the guest call on the host session bus without any prompt from Spaces. A
portal backend (xdg-desktop-portal plus the compositor's backend) may still ask the user, and
that is the only gate for the portal interfaces.

| Interface / method | Filter rule | Effect on the host | Prompt? |
|---|---|---|---|
| `org.freedesktop.ScreenSaver` `Lock` | allowed on `/org/freedesktop/ScreenSaver` and `/ScreenSaver` | locks the host session (whatever owns that name: DMS, swayidle shim, ...) | none |
| ScreenSaver `Inhibit` / `UnInhibit`, `Throttle`, `SetActive`, `SimulateUserActivity` | allowed | a guest app can keep the host awake and unlocked indefinitely, wake the screen, or switch the screen saver | none |
| `org.freedesktop.Notifications` `Notify`, `CloseNotification`, `GetCapabilities`, `GetServerInformation` | allowed on `/org/freedesktop/Notifications` | arbitrary host notifications with the host's look; the full hint set is passed through (`image-path`, `sound-file`, `desktop-entry`, actions, urgency), so a daemon that honours `image-path` or `sound-file` reads or plays host files named by the guest | none |
| `portal.Notification` | `*` | same, through the portal (icons and sounds by fd) | none |
| `portal.Inhibit` | `*` | idle/suspend/logout inhibit | backend dependent, usually none |
| `portal.Clipboard` | `*` | needs a RemoteDesktop session first, then reads and writes the host clipboard | the RemoteDesktop dialog |
| `portal.ScreenCast`, `portal.RemoteDesktop`, `portal.InputCapture` | `*` | screen capture, synthetic input and input capture of the host | the backend's consent dialog; the restore token stored for the Spaces app id makes later sessions silent |
| `portal.Usb` | `*` | enumerate and acquire host USB devices as file descriptors, which bypasses the `/dev/bus/usb` filtering of the device policy | consent dialog |
| `portal.Camera`, `Location`, `GlobalShortcuts`, `Account`, `Access`, `Email`, `Print` | `*` | media, position, shortcuts, user name/avatar, file/mail/print handoff | per backend |
| `portal.Settings` | `*` | `ReadAll` exposes the host appearance and locale-like settings namespaces | none |
| `portal.NetworkMonitor`, `ProxyResolver`, `PowerProfileMonitor` | `*` | read-only host state | none |
| `portal.OpenURI` `OpenURI`/`SchemeSupported`, `Screenshot.PickColor`, `Background.SetStatus` | per method | open links with the host handler (an unhandled scheme blocks the caller, see M4) | none for OpenURI of allowed schemes |
| `org.freedesktop.PowerManagement` queries, `StatusNotifierWatcher.RegisterStatusNotifierItem` | allowed | read power state, put tray icons in the host tray | none |
| `org.freedesktop.host.portal.Registry.Register` | allowed | the app id must be backed by the installed `spaces-open.desktop`; the Registry rejects invented ids, so a guest cannot inherit the grants of another application | none |

Observations:

1. Without a prompt the guest can lock the screen, hold it unlocked, wake it, and show or close
   notifications. These are the three that matter for a hostile guest app; none needs a portal
   decision.
2. `ScreenCast`, `RemoteDesktop`, `InputCapture` and `Usb` are only as strong as the portal
   backend's consent dialog. All of them are listed with a `*` method pattern, so every method of
   the interface is reachable, including those that need no dialog (session creation, device
   listing).
3. The Notifications rules forward hints unfiltered. Hardening would be an allow list of hints
   in the proxy filter or a broker-side `Notify` wrapper; that is a code change and out of scope
   for M5.
4. `Settings.ReadAll` and the monitor interfaces leak host state (theme, accent, proxy settings,
   network status) to the guest. Low impact.
5. The device policy is not the only route to hardware: `portal.Usb` and `portal.Camera` hand out
   file descriptors for devices the `basic` level hides. That is by design (user consent), but a
   `disabled` devices level does not imply that the guest cannot reach a camera or a USB device.
