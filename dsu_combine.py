#!/usr/bin/env python3
"""
dsu_combine.py  (auto-switching, single-slot version, debounced + stable identity)
------------------------------------------------------------------------------------

Combines two independent cemuhook/DSU servers running on the Steam Deck into
ONE virtual DSU slot (slot 0) that automatically switches source:

  - If a Steam Controller is connected -> relay ITS gyro data on slot 0.
  - If no Steam Controller is connected -> relay the Steam Deck's own
    built-in gyro on slot 0 instead.

Backends combined (both keep running exactly as they do today, unmodified,
on their normal default ports):
  - SteamDeckGyroDSU         (kmicki)              -> 127.0.0.1:26760
  - Steam-Controller-GyroDSU (dylangmarinus-stack)  -> 127.0.0.1:26761

Design notes (why these choices exist):

1. Connection state is derived from the live, continuous data stream each
   backend sends once subscribed - NOT from a fresh one-off "ports info"
   round-trip every time the emulator asks. A short debounce window means a
   single slow/dropped Bluetooth packet from the controller doesn't cause a
   momentary flash of stale Deck data.

2. Stable synthetic device identity: the Deck and the Steam Controller are
   two different real devices with two different (and differently shaped)
   MAC addresses. Some cemuhook clients (notably Dolphin and Cemu) subscribe
   by MAC address rather than by slot, and/or expect a slot's reported
   identity to stay consistent over time. If we relayed each backend's real
   MAC/model/connection-type as-is, a strict client could latch onto
   whichever device answered first and then silently ignore data that later
   arrives "from a different device" on the same slot. To avoid that, every
   outgoing packet reports one fixed, synthetic MAC/model/connection-type
   for the virtual slot, regardless of which real backend is currently
   feeding it - only the actual motion/button bytes change underneath.

3. Any subscribe request from the client (slot-based, MAC-based, or "all")
   is treated identically, since there is only ever one virtual device to
   offer. This avoids silently dropping a subscription mode a given
   emulator happens to prefer.

4. The proxy renews its own subscriptions to both backends on a timer,
   independent of how often (or whether) the emulator itself resends a
   subscribe request - some emulators subscribe once and never renew,
   which could otherwise let a backend's own internal timeout drop the
   stream out from under it.

This script does NOT re-implement gyro reading or motion math. It forwards
real requests to the real backends and relays their real replies back to
the emulator, only rewriting the slot number and identity fields described
above and recomputing the packet's CRC32. All button/motion data bytes are
passed through untouched.

Point your emulator's cemuhook / DSU server setting at:
    127.0.0.1:26759   (or whatever --listen-port you choose)
and leave it there, permanently.

Protocol reference: https://v1993.github.io/cemuhook-protocol/
"""

import asyncio
import argparse
import logging
import random
import struct
import time
import zlib

MAGIC_CLIENT = b"DSUC"
MAGIC_SERVER = b"DSUS"
PROTO_VERSION = 1001

MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_DATA = 0x100002
MSG_MOTOR_INFO = 0x110001
MSG_MOTOR_RUMBLE = 0x110002

# magic(4s) version(H) length(H) crc(I) id(I) msgtype(I) = 20 bytes total
HDR_FMT = "<4sHHIII"
HDR_LEN = struct.calcsize(HDR_FMT)

VIRTUAL_SLOT = 0
SUB_TIMEOUT_S = 8.0              # forget a client subscription if it hasn't renewed in this long
PORTS_REQUEST_TIMEOUT_S = 0.3    # cold-start-only fallback round-trip timeout
CONNECT_DEBOUNCE_S = 0.8         # grace period before falling back to the Deck gyro after the controller stream goes quiet
CACHE_MAX_AGE_S = 3.0            # how long a cached deck ports snapshot is trusted
KEEPALIVE_INTERVAL_S = 1.5       # how often the proxy renews its own subscriptions to both backends

# Stable synthetic identity presented for the virtual slot, regardless of
# which real backend is currently feeding it. 0x02 in the first octet marks
# this as a locally-administered address, so it can't collide with any real
# hardware MAC.
FIXED_MAC = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])
FIXED_MODEL = 2      # full gyro
FIXED_CONNTYPE = 2   # Bluetooth (cosmetic only)
FIXED_SERVER_ID = 0x53434F4D  # stable across restarts, so client bindings don't go stale

log = logging.getLogger("dsu-combine")


def _crc(packet_with_zeroed_crc: bytes) -> int:
    return zlib.crc32(packet_with_zeroed_crc) & 0xFFFFFFFF


def pack_dsu(magic: bytes, server_id: int, msgtype: int, payload: bytes) -> bytes:
    length = 4 + len(payload)
    zeroed = struct.pack(HDR_FMT, magic, PROTO_VERSION, length, 0, server_id, msgtype) + payload
    crc = _crc(zeroed)
    return struct.pack(HDR_FMT, magic, PROTO_VERSION, length, crc, server_id, msgtype) + payload


def parse_dsu(data: bytes):
    if len(data) < HDR_LEN:
        return None
    magic, version, length, crc, cid, msgtype = struct.unpack_from(HDR_FMT, data, 0)
    if magic not in (MAGIC_CLIENT, MAGIC_SERVER):
        return None
    return {
        "magic": magic, "version": version, "length": length,
        "crc": crc, "id": cid, "msgtype": msgtype, "payload": data[HDR_LEN:],
    }


def rebuild_as_slot0(server_id: int, msgtype: int, payload: bytes) -> bytes:
    """Force the slot number, the synthetic identity fields, and the
    connection State byte (some clients - Dolphin in particular - require
    State == 2/connected specifically to register a device at all; the
    real backends don't always set this consistently even while genuinely
    providing live data). Leave battery and everything past the shared
    header untouched."""
    payload = bytearray(payload)
    if payload:
        payload[0] = VIRTUAL_SLOT
    if len(payload) >= 2:
        payload[1] = 2  # State: connected - forced, since we only ever call
                         # this for a source we've already confirmed is live
    if len(payload) >= 10:
        payload[2] = FIXED_MODEL
        payload[3] = FIXED_CONNTYPE
        payload[4:10] = FIXED_MAC
    return pack_dsu(MAGIC_SERVER, server_id, msgtype, bytes(payload))


def not_connected_reply(server_id: int) -> bytes:
    shared = struct.pack("<BBBB6sB", VIRTUAL_SLOT, 0, 0, 0, b"\x00" * 6, 0)
    return pack_dsu(MAGIC_SERVER, server_id, MSG_PORTS, shared + b"\x00")


def ports_payload_from_shared(shared_11_bytes: bytes) -> bytes:
    """Build a valid 12-byte 'ports info' payload (shared header + padding)."""
    return bytes(shared_11_bytes[:11]) + b"\x00"


class BackendLink(asyncio.DatagramProtocol):
    def __init__(self, name, on_packet):
        self.name = name
        self.on_packet = on_packet
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.on_packet(self.name, data)

    def error_received(self, exc):
        log.warning("[%s] backend socket error: %s", self.name, exc)

    def send(self, data: bytes):
        if self.transport is not None:
            self.transport.sendto(data)


class ClientLink(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.on_packet(data, addr)

    def error_received(self, exc):
        log.warning("client-facing socket error: %s", exc)

    def send_to(self, data: bytes, addr):
        if self.transport is not None:
            self.transport.sendto(data, addr)


class Combiner:
    def __init__(self, listen_port, deck_addr, controller_addr, server_id=None, debounce=CONNECT_DEBOUNCE_S):
        self.listen_port = listen_port
        self.deck_addr = deck_addr
        self.controller_addr = controller_addr
        # Fixed, stable ID rather than a fresh random one each run: some
        # clients (Dolphin in particular) can bind an axis mapping to a
        # specific server identity the first time you configure it. A
        # different ID after every restart would make those bindings go
        # stale and silently stop updating.
        self.server_id = server_id or FIXED_SERVER_ID
        self.debounce = debounce

        self.last_controller_payload = None
        self.last_controller_ts = 0.0
        self.last_deck_payload = None
        self.last_deck_ts = 0.0
        self._active_source = None   # "deck" / "controller" / None - for transition logging only

        self.pending_ports = {}   # backend_name -> asyncio.Future (cold-start fallback only)
        self.subscribers = {}     # client_addr -> last_seen

        self.client_link: ClientLink = None
        self.backends = {}

    async def start(self):
        loop = asyncio.get_running_loop()

        _, self.client_link = await loop.create_datagram_endpoint(
            lambda: ClientLink(self.on_client_packet),
            local_addr=("0.0.0.0", self.listen_port),
        )
        _, deck_link = await loop.create_datagram_endpoint(
            lambda: BackendLink("deck", self.on_backend_packet),
            remote_addr=self.deck_addr,
        )
        _, controller_link = await loop.create_datagram_endpoint(
            lambda: BackendLink("controller", self.on_backend_packet),
            remote_addr=self.controller_addr,
        )
        self.backends = {"deck": deck_link, "controller": controller_link}

        asyncio.create_task(self._keepalive_loop())

        log.info(
            "Listening on 0.0.0.0:%d | deck=%s:%d controller=%s:%d | slot 0 auto-switches source (debounce=%.2fs)",
            self.listen_port, *self.deck_addr, *self.controller_addr, self.debounce,
        )

    async def _keepalive_loop(self):
        """Independently of whatever the emulator does, keep both backends'
        own internal subscriptions alive as long as someone is listening to
        us. This protects against emulators that subscribe once and never
        renew, which could otherwise let a backend time out its stream."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            now = time.monotonic()
            if any(now - ts < SUB_TIMEOUT_S for ts in self.subscribers.values()):
                self._resubscribe_backends()

    # ---------------- connection-state helpers ----------------

    def _controller_active(self, now=None) -> bool:
        if self.last_controller_payload is None:
            return False
        now = now if now is not None else time.monotonic()
        if now - self.last_controller_ts >= self.debounce:
            return False
        return len(self.last_controller_payload) > 11 and self.last_controller_payload[11] == 1

    # ---------------- client (emulator) side ----------------

    def on_client_packet(self, data: bytes, addr):
        pkt = parse_dsu(data)
        if pkt is None or pkt["magic"] != MAGIC_CLIENT:
            log.debug("dropped non-client packet or bad magic from %s (%d bytes)", addr, len(data))
            return
        mt = pkt["msgtype"]
        log.debug("client request from %s: msgtype=0x%06x", addr, mt)

        if mt == MSG_VERSION:
            reply = pack_dsu(MAGIC_SERVER, self.server_id, MSG_VERSION, struct.pack("<H", PROTO_VERSION))
            self.client_link.send_to(reply, addr)

        elif mt == MSG_PORTS:
            asyncio.create_task(self._handle_ports_request(addr))

        elif mt == MSG_DATA:
            # There is only one virtual device to offer, so any subscribe
            # request - whether slot-based, MAC-based, or "all controllers"
            # - means the same thing: "send me that one device's data."
            self.subscribers[addr] = time.monotonic()
            self._resubscribe_backends()

        elif mt == MSG_MOTOR_INFO:
            shared = struct.pack("<BBBB6sB", 0, 0, 0, 0, b"\x00" * 6, 0)
            reply = pack_dsu(MAGIC_SERVER, self.server_id, MSG_MOTOR_INFO, shared + b"\x00")
            self.client_link.send_to(reply, addr)

        # MSG_MOTOR_RUMBLE: no-op, nothing to rumble.

    def _resubscribe_backends(self):
        req_payload = bytes([0x01, 0x00]) + b"\x00" * 6  # slot-based, real slot 0, on both backends
        req = pack_dsu(MAGIC_CLIENT, self.server_id, MSG_DATA, req_payload)
        for name, link in self.backends.items():
            log.debug("sending subscribe request to backend '%s' (%s)", name, link.transport.get_extra_info("peername") if link.transport else "no transport yet")
            link.send(req)

    async def _handle_ports_request(self, addr):
        try:
            await self._handle_ports_request_inner(addr)
        except Exception:
            log.exception("unhandled error while handling ports-info request from %s", addr)

    async def _handle_ports_request_inner(self, addr):
        now = time.monotonic()

        if self._controller_active(now):
            payload = ports_payload_from_shared(self.last_controller_payload)
            reply = rebuild_as_slot0(self.server_id, MSG_PORTS, payload)
            log.debug("ports-info reply to %s: source=controller(cached) state=%d model=%d bytes=%s",
                      addr, payload[1], payload[2], reply.hex())
            self.client_link.send_to(reply, addr)
            return

        if self.last_deck_payload is not None and (now - self.last_deck_ts) < CACHE_MAX_AGE_S:
            payload = ports_payload_from_shared(self.last_deck_payload)
            reply = rebuild_as_slot0(self.server_id, MSG_PORTS, payload)
            log.debug("ports-info reply to %s: source=deck(cached) state=%d model=%d bytes=%s",
                      addr, payload[1], payload[2], reply.hex())
            self.client_link.send_to(reply, addr)
            return

        # Cold start: no streaming data cached yet at all - fall back to a
        # one-off round trip so the very first query still gets a real answer.
        log.debug("ports-info request from %s: no cached data yet, doing cold-start round trip", addr)
        controller_reply = await self._query_backend_ports("controller")
        if controller_reply is not None:
            payload, state = controller_reply
            if state == 2:
                reply = rebuild_as_slot0(self.server_id, MSG_PORTS, payload)
                log.debug("ports-info reply to %s: source=controller(cold-start) state=%d bytes=%s",
                          addr, state, reply.hex())
                self.client_link.send_to(reply, addr)
                return

        deck_reply = await self._query_backend_ports("deck")
        if deck_reply is not None:
            payload, _state = deck_reply
            reply = rebuild_as_slot0(self.server_id, MSG_PORTS, payload)
            log.debug("ports-info reply to %s: source=deck(cold-start) bytes=%s", addr, reply.hex())
            self.client_link.send_to(reply, addr)
            return

        log.debug("ports-info reply to %s: NOT CONNECTED (no backend answered at all)", addr)
        self.client_link.send_to(not_connected_reply(self.server_id), addr)


    async def _query_backend_ports(self, backend_name):
        fut = asyncio.get_running_loop().create_future()
        self.pending_ports[backend_name] = fut
        req_payload = struct.pack("<iB", 1, 0)  # ask for real slot 0
        req = pack_dsu(MAGIC_CLIENT, self.server_id, MSG_PORTS, req_payload)
        self.backends[backend_name].send(req)
        try:
            raw = await asyncio.wait_for(fut, timeout=PORTS_REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.pending_ports.pop(backend_name, None)
            return None
        inner = parse_dsu(raw)
        payload = inner["payload"]
        state = payload[1] if len(payload) > 1 else 0
        return payload, state

    # ---------------- backend (real server) side ----------------

    def on_backend_packet(self, backend_name: str, data: bytes):
        pkt = parse_dsu(data)
        if pkt is None or pkt["magic"] != MAGIC_SERVER:
            log.debug("dropped unparsable/bad-magic packet from backend '%s' (%d bytes)", backend_name, len(data))
            return
        log.debug("backend '%s' sent msgtype=0x%06x", backend_name, pkt["msgtype"])

        if pkt["msgtype"] == MSG_PORTS:
            fut = self.pending_ports.pop(backend_name, None)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return

        if pkt["msgtype"] == MSG_DATA:
            payload = pkt["payload"]
            if len(payload) < 12:
                return

            now = time.monotonic()

            if backend_name == "controller":
                if self.last_controller_payload is None:
                    log.debug("[controller] first raw data packet ever seen from this backend (connected=%d)", payload[11])
                self.last_controller_payload = payload
                self.last_controller_ts = now
                active_now = self._controller_active(now)
                if not active_now:
                    if self._active_source == "controller":
                        log.info("switching source: controller -> deck")
                        self._active_source = "deck"
                    return  # not currently the active source, stay quiet
                if self._active_source != "controller":
                    log.info("switching source: %s -> controller", self._active_source)
                    self._active_source = "controller"
            else:  # backend_name == "deck"
                self.last_deck_payload = payload
                self.last_deck_ts = now
                if self._controller_active(now):
                    return  # controller currently owns the slot, ignore deck data
                if self._active_source != "deck":
                    log.info("switching source: %s -> deck", self._active_source)
                    self._active_source = "deck"

            out = rebuild_as_slot0(self.server_id, MSG_DATA, payload)
            for caddr, ts in list(self.subscribers.items()):
                if now - ts < SUB_TIMEOUT_S:
                    self.client_link.send_to(out, caddr)
                else:
                    del self.subscribers[caddr]


async def main():
    ap = argparse.ArgumentParser(
        description="Combine SteamDeckGyroDSU + Steam-Controller-GyroDSU into one auto-switching DSU slot."
    )
    ap.add_argument("--listen-port", type=int, default=26759,
                     help="Port emulators should point at (default: 26759)")
    ap.add_argument("--deck-host", default="127.0.0.1")
    ap.add_argument("--deck-port", type=int, default=26760,
                     help="SteamDeckGyroDSU port (default: 26760, its own default)")
    ap.add_argument("--controller-host", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=26761,
                     help="Steam-Controller-GyroDSU port (default: 26761, its own default)")
    ap.add_argument("--debounce", type=float, default=CONNECT_DEBOUNCE_S,
                     help="Seconds of grace before falling back to the Deck gyro after the "
                          "controller stream goes quiet (default: %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    combiner = Combiner(
        args.listen_port,
        (args.deck_host, args.deck_port),
        (args.controller_host, args.controller_port),
        debounce=args.debounce,
    )
    await combiner.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
