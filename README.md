# steamdeck-dsu-combiner

A single, permanent cemuhook/DSU server address for your Steam Deck - no more
switching your emulator's motion-control settings depending on whether a
Steam Controller happens to be connected.

## The problem

[SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU) exposes the
Deck's own built-in gyro as a cemuhook (DSU) server on port `26760`.
[Steam-Controller-GyroDSU](https://github.com/dylangmarinus-stack/Steam-Controller-GyroDSU)
does the same for an external Steam Controller, on port `26761`. Both are
great, and they're explicitly designed to run side by side - but that means
your emulator needs **two** separate DSU server entries, and most emulators
only expose one motion source per game profile. In practice this meant
manually flipping which port an emulator pointed at depending on whether the
Steam Controller happened to be connected that session - annoying, and easy
to forget.

## The fix

This is a small, dependency-free Python proxy that sits in front of both
servers and exposes **one** virtual DSU device (slot 0) on a single port. It
watches both real backends live and automatically relays whichever one is
actually active:

- Steam Controller connected and producing motion → relayed as slot 0.
- Not connected → falls back to the Deck's own gyro on the same slot 0.

Your emulator only ever needs one cemuhook server entry, configured once,
forever: `127.0.0.1:26759`.

It's a protocol-level relay, not a reimplementation of gyro reading - all
actual motion/button bytes come straight from the real backends, untouched.
The proxy only rewrites the slot number, a small stable identity block (see
[How it works](#how-it-works)), and recomputes the packet's CRC32.

## Requirements

- A Steam Deck (or any SteamOS/Linux system) with both of the following
  already installed and running as `systemctl --user` services on their
  default ports:
  - [SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU) → `sdgyrodsu.service`, port `26760`
  - [Steam-Controller-GyroDSU](https://github.com/dylangmarinus-stack/Steam-Controller-GyroDSU) → `SteamControllerGyroDSU.service`, port `26761`
- Python 3 (already present on SteamOS, no extra packages required)

## Install

```bash
git clone https://github.com/korph-jz/steamdeck-dsu-combiner.git
cd steamdeck-dsu-combiner
./install.sh
```

This copies the script to `~/dsu-combine/`, installs the systemd user
service, and starts it immediately.

Then point every emulator's cemuhook/DSU setting at:

```
127.0.0.1:26759
```

...and never change it again.

## Uninstall

```bash
./uninstall.sh
```

This only removes the combiner itself. SteamDeckGyroDSU and
Steam-Controller-GyroDSU are separate projects with their own uninstallers.

## Usage / options

The service runs `dsu_combine.py` with no arguments by default. All of these
are optional:

| Flag | Default | Meaning |
|---|---|---|
| `--listen-port` | `26759` | Port emulators should point at |
| `--deck-host` / `--deck-port` | `127.0.0.1` / `26760` | SteamDeckGyroDSU's address |
| `--controller-host` / `--controller-port` | `127.0.0.1` / `26761` | Steam-Controller-GyroDSU's address |
| `--debounce` | `0.8` | Seconds of grace before falling back to the Deck's gyro after the controller stream goes quiet |
| `-v` / `--verbose` | off | Debug-level logging (see [Troubleshooting](#troubleshooting)) |

Edit `ExecStart=` in `dsu-combine.service` to change any of these
permanently, then `systemctl --user daemon-reload && systemctl --user restart dsu-combine.service`.

## Emulator-specific setup notes

The general pattern for every emulator below is the same: add
`127.0.0.1:26759` as a cemuhook/DSU server (once), make sure it's on
port/slot `0`, and - this is the part that trips people up - **motion
input and regular buttons/sticks are configured completely separately.**
This proxy (like both upstream projects) only ever carries gyro/accelerometer
data, never button state, so it should only ever be selected as the device
for the motion-specific fields, never for general controls.

### Dolphin

1. Controllers → Alternate Input Sources → **DSU Client** tab → check
   **Enable** → **Add** → enter `127.0.0.1` / `26759`.
2. Open a Wii Remote's **Configure** window → **Motion Input** tab. At the
   top of *that tab specifically* there's a Device dropdown - select
   `DSUClient/0/...`. The Accelerometer/Gyroscope fields should auto-populate;
   if they don't, right-click each one and pick the matching axis from the
   list.
3. Switch to the **General** tab in that *same* window (regular
   buttons/D-pad) and change its Device dropdown back to the controller's
   own native entry (e.g. an `SDL/...` entry) - **not** `DSUClient`. Bind
   A/B/1/2/etc. here.
4. For GameCube games, the GameCube controller's own Configure screen has
   its own independent Device dropdown - same rule applies (native device
   for buttons; DSU is only relevant to Wii Remote motion, GameCube
   controllers don't have gyro).
5. If `DSUClient/0/...` never appears in the Device dropdown at all: fully
   restart Dolphin (not just the Controllers window) with the proxy and a
   controller already active first - Dolphin appears to only scan for DSU
   devices at certain moments, not live while you edit the server list.

### Cemu

1. Input Settings → Add Input API → **API: DSUController**, **Controller:
   Controller 1**, IP `127.0.0.1`, Port `26759` → Add.
2. That's it for motion - Cemu treats the DSU entry as motion-only
   automatically. Bind regular buttons/sticks the normal way, using a
   separate input API entry (e.g. **Controller API: SDLController**) for
   the same physical controller.

### Eden / Azahar (3DS) and other simpler cemuhook clients

These generally just have a single "CemuHook (DSU) motion server" address
field with no separate device/slot concept to worry about - point it at
`127.0.0.1:26759` and it should work without the extra steps above.

## How it works

- **One virtual slot.** Since you only ever physically hold one device at a
  time (the Deck sits still while you're holding the Steam Controller),
  there's no need to expose multiple slots - the proxy always reports as
  slot 0, and decides internally which real backend currently "owns" it.
- **Debounced switching.** Connection state is derived from the live,
  continuous data stream each backend sends, not a fresh round-trip query
  every time the emulator asks. A short debounce window (default 0.8s)
  means a single slow/dropped Bluetooth packet doesn't cause a momentary
  flash back to stale Deck data.
- **Stable synthetic identity.** The Deck and the Steam Controller are two
  different real devices with two different MAC addresses. Some clients
  (Dolphin, Cemu) can key a device binding off that identity, or require the
  "connection state" field to read a specific value. Every outgoing packet
  reports one fixed, synthetic MAC/model/connection-type/server-ID for the
  virtual slot, and always reports "connected" whenever real motion data is
  being relayed - regardless of which real backend is behind it or what
  that backend's own raw fields happen to say. This is required in practice:
  Steam-Controller-GyroDSU's own data stream reports its "State" byte as `1`
  (reserved) rather than `2` (connected) even while genuinely active, which
  is harmless for some clients but makes Dolphin refuse to register a
  device at all until it's corrected.
- **Proxy-driven keep-alive.** The proxy renews its own subscriptions to
  both backends on a timer, independent of how often (or whether) a given
  emulator resends a subscribe request - some emulators subscribe once and
  never renew.

## Known limitations

- **Motion only.** Like both upstream projects, this combiner only ever
  carries gyro/accelerometer data. It does not, and cannot, carry
  button/stick state. Bind regular controls to the controller's own native
  device entry (SDL/evdev) in your emulator, separate from the DSU device
  used for motion.
- **Steam Input conflict.** If Steam Input has claimed the Steam Controller
  (the default for most non-Steam-game shortcuts), Steam-Controller-GyroDSU
  cannot read its raw gyro data, and this proxy will simply and correctly
  keep falling back to the Deck's own gyro - it has no way to make Steam
  release the device. If you want live gyro from the actual Steam
  Controller, that specific shortcut needs Steam Input fully disabled
  (not just switched to "Desktop Configuration", which is itself still a
  Steam Input profile). This does trade away Steam Input's custom bindings
  and community configs for that shortcut; most modern emulators (built on
  SDL2) can still read the controller's buttons/sticks natively once Steam
  Input is out of the way, so you'd rebind buttons in the emulator itself
  rather than lose them outright - but it's worth testing before assuming
  either way.
- **Only the first controller.** This combiner intentionally exposes one
  slot. If you need multiple Steam Controllers *and* the Deck's gyro
  available simultaneously as separate slots, this project isn't the right
  fit as-is.

## Troubleshooting

Run the script manually with `-v` for live debug output (stop the service
first so the port isn't in use twice):

```bash
systemctl --user stop dsu-combine.service
python3 ~/dsu-combine/dsu_combine.py -v
```

- **Emulator never detects any device at all.** Some emulators only scan
  for DSU devices at launch - make sure the proxy (and, ideally, the
  controller) are already running *before* you open the emulator, and try
  a full restart of the emulator rather than just reopening its controller
  settings.
- **Always shows the Deck's motion, never the controller's.** Watch the
  verbose log for `switching source: ...` lines while moving the
  controller. If that line never appears, the controller backend isn't
  reaching the proxy at all - check `systemctl --user status
  SteamControllerGyroDSU.service` and its own logs directly. If it does
  appear and stays on `controller`, the pipe is fine and the remaining
  issue is on the emulator's own binding/device-selection side.
- **A specific emulator's device dropdown stays empty even though the log
  looks healthy.** Different DSU clients have different strictness. This
  proxy already works around the one such quirk found in the wild -
  Dolphin's requirement that the "State" byte read exactly "connected" -
  but if you hit another one, please open an issue with your `-v` log
  output around the moment of the failed request/reply.
- **Make sure your emulator's "port"/"slot" field is set to `0`** wherever
  it's configurable - this proxy only ever reports on slot 0.

## Credits

- [kmicki/SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU)
- [dylangmarinus-stack/Steam-Controller-GyroDSU](https://github.com/dylangmarinus-stack/Steam-Controller-GyroDSU)
- [DSU/cemuhook protocol reference](https://v1993.github.io/cemuhook-protocol/)

This project doesn't include or modify any code from either upstream
project - it's an independent protocol-level relay that runs alongside them,
built from the public DSU protocol spec.

Implementation and debugging built with the help of Claude (Anthropic).

## License

MIT - see [LICENSE](LICENSE).
