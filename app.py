"""BadgeBlud: a multiplayer ray-cast FPS for the EMF 2026 Tildagon."""

import math
import time

import app
from events.input import ButtonDownEvent, ButtonUpEvent
from system.eventbus import eventbus
from system.espnow import BROADCAST_MAC, EspNowReceiveEvent, espnow_service

try:
    import network
except ImportError:  # Host tests provide no ESP32 network module.
    network = None

try:
    import settings
except ImportError:
    settings = None

try:
    import wifi as badge_wifi
except ImportError:
    badge_wifi = None

try:
    from apps.badgeblud import engine, protocol
except ImportError:  # Host package import.
    from . import engine, protocol


MENU = 0
HOST_LOBBY = 1
JOIN_BROWSER = 2
CLIENT_LOBBY = 3
MATCH = 4
RESULTS = 5

MATCH_LENGTH_MS = 180000
COUNTDOWN_MS = 3000
FIRE_COOLDOWN_MS = 350
RESPAWN_MS = 2000
SPAWN_PROTECTION_MS = 1000
PEER_TIMEOUT_MS = 3000
POSE_INTERVAL_MS = 100
SNAPSHOT_INTERVAL_MS = 100
BEACON_INTERVAL_MS = 500
SHOT_STALE_MS = 700
GAME_CHANNEL = 6
DAMAGE = 100
REMOTE_SMOOTH_RATE = 10.0

PLAYER_COLOURS = (
    (0.20, 0.92, 1.00),
    (1.00, 0.24, 0.62),
    (1.00, 0.76, 0.12),
    (0.40, 1.00, 0.28),
)

WALL_COLOURS = (
    (0.12, 0.82, 0.94),
    (0.10, 0.68, 0.82),
    (0.09, 0.55, 0.70),
    (0.08, 0.43, 0.58),
    (0.07, 0.32, 0.46),
    (0.06, 0.23, 0.35),
)


def _ticks_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.monotonic() * 1000)


def _ticks_diff(left, right):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(left, right)
    return left - right


def _ticks_add(value, delta):
    if hasattr(time, "ticks_add"):
        return time.ticks_add(value, delta)
    return value + delta


def _sleep_ms(milliseconds):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(milliseconds)
    else:
        time.sleep(milliseconds * 0.001)


def _touch_number(name):
    if not name.startswith("TOUCH"):
        return None
    try:
        number = int(name[5:])
    except ValueError:
        return None
    return number if 1 <= number <= 12 else None


def _touch_step(previous, current):
    """Return -1/1 for adjacent counter-clockwise/clockwise touch motion."""
    difference = (current - previous) % 12
    if difference == 1:
        return 1
    if difference == 11:
        return -1
    return 0


def _sequence_is_new(sequence, previous):
    if previous is None:
        return True
    distance = (sequence - previous) & 0xFFFF
    return 0 < distance < 0x8000


class Player:
    __slots__ = (
        "slot",
        "mac",
        "name",
        "x",
        "y",
        "heading",
        "render_x",
        "render_y",
        "render_heading",
        "health",
        "kills",
        "deaths",
        "streak",
        "dead",
        "protected_until",
        "respawn_at",
        "last_seen",
        "last_fire",
        "map_open",
    )

    def __init__(self, slot, mac, name, now=0):
        self.slot = slot
        self.mac = mac
        self.name = name
        self.x, self.y, self.heading = engine.SPAWNS[slot]
        self.render_x = self.x
        self.render_y = self.y
        self.render_heading = self.heading
        self.health = 100
        self.kills = 0
        self.deaths = 0
        self.streak = 0
        self.dead = False
        self.protected_until = 0
        self.respawn_at = 0
        self.last_seen = now
        self.last_fire = None
        self.map_open = False

    def flags(self, now):
        flags = 0
        if self.dead:
            flags |= protocol.FLAG_DEAD
        if self.protected_until and _ticks_diff(self.protected_until, now) > 0:
            flags |= protocol.FLAG_PROTECTED
        if self.map_open:
            flags |= protocol.FLAG_MAP_OPEN
        return flags

    def snapshot(self, now):
        respawn = 0
        if self.dead:
            respawn = max(0, _ticks_diff(self.respawn_at, now))
        return {
            "slot": self.slot,
            "mac": self.mac,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "heading": self.heading,
            "health": self.health,
            "kills": self.kills,
            "deaths": self.deaths,
            "streak": self.streak,
            "flags": self.flags(now),
            "respawn_ms": respawn,
        }


class BadgeBludApp(app.App):
    """Host-authoritative local-radio deathmatch."""

    def __init__(self):
        super().__init__()
        eventbus.on(ButtonDownEvent, self._button_down, self)
        eventbus.on(ButtonUpEvent, self._button_up, self)

        self.held = set()
        self.pressed = set()
        self.touch_held = set()
        self.last_touch = None
        self.last_touch_time = 0

        self.mode = MENU
        self.menu_index = 0
        self.status = "HOST OR JOIN"
        self.is_host = False
        self.session = 0
        self.sequence = 0
        self.local_slot = 0
        self.local_mac = self._read_local_mac()
        self.local_name = self._read_local_name()
        self.host_mac = None
        self.players = {}
        self.mac_to_slot = {}
        self.hosts = {}
        self.host_selection = 0
        self.last_sequences = {}
        self.last_host_seen = 0

        self.player_x, self.player_y, self.player_heading = engine.SPAWNS[0]
        self.map_open = False
        self.countdown_ms = 0
        self.remaining_ms = MATCH_LENGTH_MS
        self.winner = None
        self.raycaster = engine.RayCaster()
        self.muzzle_ms = 0
        self.hit_ms = 0
        self.notice = ""

        now = _ticks_ms()
        self.last_beacon = now - BEACON_INTERVAL_MS
        self.last_pose = now - POSE_INTERVAL_MS
        self.last_snapshot = now - SNAPSHOT_INTERVAL_MS
        self.radio_handler = None
        self.radio_channel_claimed = False
        self.original_wifi_channel = None
        self.wifi_was_connected = False

    @staticmethod
    def _read_local_name():
        try:
            return protocol.decode_name(protocol.clean_name(settings.get("name", "badge")))
        except Exception:
            return "badge"

    @staticmethod
    def _read_local_mac():
        try:
            station = network.WLAN(network.STA_IF)
            station.active(True)
            value = station.config("mac")
            if isinstance(value, bytes) and len(value) == 6:
                return value
        except Exception:
            pass
        return b"\x00\x00\x00\x00\x00\x00"

    def _wifi_channel(self):
        try:
            return espnow_service.wifi_channel or 0
        except Exception:
            return 0

    def _ensure_radio(self):
        if self.radio_handler is not None:
            return
        try:
            self.radio_handler = espnow_service.subscribe(
                self._on_radio,
                self,
                predicate=lambda event: event.msg.startswith(protocol.MAGIC),
            )
        except Exception as error:
            self.radio_handler = None
            self.status = "RADIO: %s" % error

    def _release_radio(self):
        if self.radio_handler is None:
            return
        try:
            eventbus.remove(EspNowReceiveEvent, self.radio_handler, self)
        except Exception:
            pass
        self.radio_handler = None

    def _claim_game_channel(self):
        """Put ESP-NOW peers on one channel for the lifetime of a session."""
        if self.radio_channel_claimed:
            return True
        try:
            station = network.WLAN(network.STA_IF)
            station.active(True)
            self.original_wifi_channel = station.config("channel")
            self.wifi_was_connected = station.isconnected()
            self.radio_channel_claimed = True
            # isconnected() is false while the driver is still attempting a
            # connection, so always cancel pending station work before changing
            # the ESP-NOW channel.
            try:
                station.disconnect()
            except Exception:
                pass
            last_error = None
            for _ in range(5):
                _sleep_ms(50)
                try:
                    station.config(channel=GAME_CHANNEL)
                    if station.config("channel") == GAME_CHANNEL:
                        return True
                    last_error = OSError("channel did not change")
                except Exception as error:
                    last_error = error
                    try:
                        station.disconnect()
                    except Exception:
                        pass
            raise last_error or OSError("channel unavailable")
        except Exception as error:
            message = "CHANNEL ERROR: %s" % error
            self._restore_wifi_channel()
            self.status = message
            return False

    def _restore_wifi_channel(self):
        if not self.radio_channel_claimed:
            return
        try:
            station = network.WLAN(network.STA_IF)
            if self.original_wifi_channel:
                station.config(channel=self.original_wifi_channel)
            if self.wifi_was_connected and badge_wifi is not None:
                badge_wifi.connect()
        except Exception as error:
            self.notice = "WIFI RESTORE %s" % error
        self.radio_channel_claimed = False
        self.original_wifi_channel = None
        self.wifi_was_connected = False

    def _next_sequence(self):
        self.sequence = (self.sequence + 1) & 0xFFFF
        return self.sequence

    def _send(self, data, mac=BROADCAST_MAC):
        try:
            espnow_service.send(data, mac)
            return True
        except Exception as error:
            self.notice = "RADIO %s" % error
            return False

    def _send_to_clients(self, data, repeats=1):
        """Unicast critical host messages so ESP-NOW can acknowledge them."""
        for _ in range(repeats):
            for slot, player in self.players.items():
                if slot != self.local_slot:
                    self._send(data, player.mac)

    def _button_down(self, event):
        name = event.button.name
        if name in self.held:
            return
        self.held.add(name)
        self.pressed.add(name)
        number = _touch_number(name)
        if number is not None:
            self.touch_held.add(number)
            now = _ticks_ms()
            if (
                self.mode == MATCH
                and self.countdown_ms <= 0
                and self.last_touch is not None
                and _ticks_diff(now, self.last_touch_time) <= 450
            ):
                self.player_heading = engine.wrap_angle(
                    self.player_heading + _touch_step(self.last_touch, number) * 0.045
                )
            self.last_touch = number
            self.last_touch_time = now

    def _button_up(self, event):
        name = event.button.name
        self.held.discard(name)
        number = _touch_number(name)
        if number is not None:
            self.touch_held.discard(number)

    def _clear_inputs(self):
        self.held.clear()
        self.pressed.clear()
        self.touch_held.clear()
        self.last_touch = None

    def minimise(self):
        self._leave_session()
        self.mode = MENU
        self.menu_index = 0
        self.status = "HOST OR JOIN"
        self._clear_inputs()
        self._release_radio()
        super().minimise()

    def _leave_session(self):
        if self.session and self.mode != MENU:
            target = BROADCAST_MAC if self.is_host else self.host_mac
            if target is not None:
                self._send(
                    protocol.pack_leave(self.session, self._next_sequence()), target
                )
        self.is_host = False
        self.session = 0
        self.players.clear()
        self.mac_to_slot.clear()
        self.last_sequences.clear()
        self.map_open = False
        self._release_radio()
        self._restore_wifi_channel()

    def _reset_to_menu(self, status="HOST OR JOIN"):
        self._leave_session()
        self.mode = MENU
        self.menu_index = 0
        self.status = status
        self.host_mac = None
        self.hosts.clear()
        self.winner = None

    def _start_host(self):
        if not self._claim_game_channel():
            return
        self._ensure_radio()
        if self.radio_handler is None:
            self._restore_wifi_channel()
            return
        now = _ticks_ms()
        seed = now
        for value in self.local_mac:
            seed = ((seed << 5) ^ value ^ (seed >> 3)) & 0xFFFF
        self.session = seed or 1
        self.is_host = True
        self.mode = HOST_LOBBY
        self.status = "WAITING FOR PLAYERS"
        self.local_slot = 0
        local = Player(0, self.local_mac, self.local_name, now)
        self.players = {0: local}
        self.mac_to_slot = {self.local_mac: 0}
        self._use_local_spawn(0)
        self.last_beacon = now - BEACON_INTERVAL_MS

    def _start_browser(self):
        self._leave_session()
        if not self._claim_game_channel():
            return
        self._ensure_radio()
        if self.radio_handler is None:
            self._restore_wifi_channel()
            return
        self.mode = JOIN_BROWSER
        self.hosts = {}
        self.host_selection = 0
        self.status = "SCANNING"

    def _selected_hosts(self):
        return sorted(self.hosts.items(), key=lambda item: item[1]["name"])

    def _join_selected(self):
        hosts = self._selected_hosts()
        if not hosts:
            self.status = "NO HOSTS FOUND"
            return
        self.host_selection %= len(hosts)
        mac, details = hosts[self.host_selection]
        self.host_mac = mac
        self.session = details["session"]
        self.is_host = False
        self.mode = CLIENT_LOBBY
        self.status = "JOINING %s" % details["name"]
        self.last_host_seen = _ticks_ms()
        self._send(
            protocol.pack_join(self.session, self._next_sequence(), self.local_name),
            mac,
        )

    def _begin_match(self):
        if not self.is_host or len(self.players) < 2:
            self.status = "NEED 2 PLAYERS"
            return
        now = _ticks_ms()
        for slot, player in self.players.items():
            player.x, player.y, player.heading = engine.SPAWNS[slot]
            player.render_x = player.x
            player.render_y = player.y
            player.render_heading = player.heading
            player.health = 100
            player.kills = 0
            player.deaths = 0
            player.streak = 0
            player.dead = False
            player.protected_until = _ticks_add(now, SPAWN_PROTECTION_MS)
            player.respawn_at = 0
            player.map_open = False
        self._use_local_spawn(self.local_slot)
        self.mode = MATCH
        self.map_open = False
        self.countdown_ms = COUNTDOWN_MS
        self.remaining_ms = MATCH_LENGTH_MS
        self.winner = None
        start = protocol.pack_start(
            self.session, self._next_sequence(), COUNTDOWN_MS
        )
        self._send_to_clients(start, repeats=2)
        self._broadcast_snapshot(now)

    def _use_local_spawn(self, slot, death_count=0):
        spawn = engine.SPAWNS[(slot + death_count) % len(engine.SPAWNS)]
        self.player_x, self.player_y, self.player_heading = spawn

    def _find_free_slot(self):
        for slot in range(protocol.MAX_PLAYERS):
            if slot not in self.players:
                return slot
        return None

    def _on_radio(self, event):
        try:
            message_type, session, sequence, payload = protocol.parse(event.msg)
        except Exception:
            return
        now = _ticks_ms()

        if message_type == protocol.BEACON:
            try:
                phase, count, channel, name = protocol.unpack_beacon(payload)
            except protocol.ProtocolError:
                return
            if self.mode == JOIN_BROWSER and phase == protocol.PHASE_LOBBY:
                self.hosts[event.mac] = {
                    "session": session,
                    "count": count,
                    "channel": channel,
                    "name": name,
                    "seen": now,
                }
                self.status = "HOSTS FOUND %d" % len(self.hosts)
            elif (
                self.mode == CLIENT_LOBBY
                and event.mac == self.host_mac
                and session == self.session
                and phase == protocol.PHASE_LOBBY
            ):
                # Lobby beacons are the host heartbeat while we wait for START.
                self.last_host_seen = now
            return

        if session != self.session or not self.session:
            return
        key = (event.mac, message_type)
        previous = self.last_sequences.get(key)
        if not _sequence_is_new(sequence, previous):
            return
        self.last_sequences[key] = sequence

        if self.is_host:
            self._on_host_message(event.mac, message_type, payload, now)
        elif event.mac == self.host_mac:
            self.last_host_seen = now
            self._on_client_message(message_type, payload, now)

    def _on_host_message(self, mac, message_type, payload, now):
        if message_type == protocol.JOIN:
            if self.mode != HOST_LOBBY:
                self._send(
                    protocol.pack_reject(
                        self.session, self._next_sequence(), protocol.REJECT_STARTED
                    ),
                    mac,
                )
                return
            try:
                name = protocol.unpack_join(payload)
            except protocol.ProtocolError:
                return
            if mac in self.mac_to_slot:
                slot = self.mac_to_slot[mac]
            else:
                slot = self._find_free_slot()
                if slot is None:
                    self._send(
                        protocol.pack_reject(
                            self.session, self._next_sequence(), protocol.REJECT_FULL
                        ),
                        mac,
                    )
                    return
                self.players[slot] = Player(slot, mac, name, now)
                self.mac_to_slot[mac] = slot
            self.players[slot].last_seen = now
            self._send(protocol.pack_accept(self.session, self._next_sequence(), slot), mac)
            self.status = "%d/4 READY" % len(self.players)
            return

        slot = self.mac_to_slot.get(mac)
        if slot is None:
            return
        player = self.players.get(slot)
        if player is None:
            return
        player.last_seen = now

        if message_type == protocol.POSE and self.mode == MATCH:
            try:
                x, y, heading, flags = protocol.unpack_pose(payload)
            except protocol.ProtocolError:
                return
            if engine.position_is_open(x, y):
                player.x = x
                player.y = y
            player.heading = heading
            dx = player.x - player.render_x
            dy = player.y - player.render_y
            if dx * dx + dy * dy > 16.0:
                player.render_x = player.x
                player.render_y = player.y
                player.render_heading = player.heading
            player.map_open = bool(flags & protocol.FLAG_MAP_OPEN)
        elif message_type == protocol.FIRE and self.mode == MATCH:
            try:
                heading = protocol.unpack_fire(payload)
            except protocol.ProtocolError:
                return
            self._resolve_shot(slot, heading, now)
        elif message_type == protocol.LEAVE:
            self._remove_player(slot)

    def _on_client_message(self, message_type, payload, now):
        if message_type == protocol.ACCEPT and self.mode == CLIENT_LOBBY:
            try:
                self.local_slot = protocol.unpack_accept(payload)
            except protocol.ProtocolError:
                return
            self.status = "JOINED AS P%d" % (self.local_slot + 1)
            self._use_local_spawn(self.local_slot)
        elif message_type == protocol.REJECT:
            try:
                reason = protocol.unpack_reject(payload)
            except protocol.ProtocolError:
                return
            labels = {
                protocol.REJECT_FULL: "LOBBY FULL",
                protocol.REJECT_STARTED: "MATCH STARTED",
                protocol.REJECT_VERSION: "VERSION MISMATCH",
            }
            self._reset_to_menu(labels.get(reason, "JOIN REJECTED"))
        elif message_type == protocol.START:
            try:
                self.countdown_ms = protocol.unpack_start(payload)
            except protocol.ProtocolError:
                return
            self.mode = MATCH
            self.map_open = False
            self.remaining_ms = MATCH_LENGTH_MS
            self._use_local_spawn(self.local_slot)
        elif message_type == protocol.SNAPSHOT and self.mode in (CLIENT_LOBBY, MATCH):
            was_waiting = self.mode == CLIENT_LOBBY
            try:
                self._apply_snapshot(payload, now)
            except protocol.ProtocolError:
                return
            if was_waiting:
                # A snapshot is also a recovery path if both START copies were lost.
                self.mode = MATCH
                self.countdown_ms = 0
        elif message_type == protocol.HIT and self.mode == MATCH:
            try:
                attacker, victim, health = protocol.unpack_hit(payload)
            except protocol.ProtocolError:
                return
            if victim == self.local_slot:
                self.hit_ms = 180
                self.notice = "HIT BY P%d" % (attacker + 1)
            elif attacker == self.local_slot:
                self.notice = "HIT P%d" % (victim + 1)
        elif message_type == protocol.END:
            try:
                self.winner = protocol.unpack_end(payload)
            except protocol.ProtocolError:
                return
            self.mode = RESULTS
        elif message_type == protocol.LEAVE:
            self._reset_to_menu("HOST LEFT")

    def _apply_snapshot(self, payload, now):
        remaining, snapshots = protocol.unpack_snapshot(payload)
        self.remaining_ms = remaining
        seen = set()
        for data in snapshots:
            slot = data["slot"]
            seen.add(slot)
            player = self.players.get(slot)
            is_new = player is None
            was_dead = player.dead if player is not None else False
            if player is None:
                player = Player(slot, data["mac"], data["name"], now)
                self.players[slot] = player
            player.mac = data["mac"]
            player.name = data["name"]
            player.x = data["x"]
            player.y = data["y"]
            player.heading = data["heading"]
            player.health = data["health"]
            player.kills = data["kills"]
            player.deaths = data["deaths"]
            player.streak = data["streak"]
            player.dead = bool(data["flags"] & protocol.FLAG_DEAD)
            player.map_open = bool(data["flags"] & protocol.FLAG_MAP_OPEN)
            player.last_seen = now
            if slot == self.local_slot:
                if was_dead and not player.dead:
                    self.player_x = player.x
                    self.player_y = player.y
                    self.player_heading = player.heading
                elif not player.dead and not engine.position_is_open(
                    self.player_x, self.player_y
                ):
                    self.player_x = player.x
                    self.player_y = player.y
                player.x = self.player_x
                player.y = self.player_y
                player.heading = self.player_heading
                player.render_x = self.player_x
                player.render_y = self.player_y
                player.render_heading = self.player_heading
            elif is_new or was_dead != player.dead:
                player.render_x = player.x
                player.render_y = player.y
                player.render_heading = player.heading
        for slot in tuple(self.players):
            if slot not in seen:
                del self.players[slot]

    def _remove_player(self, slot):
        player = self.players.pop(slot, None)
        if player is not None:
            self.mac_to_slot.pop(player.mac, None)
        self.status = "%d/4 READY" % len(self.players)

    def _local_player(self):
        return self.players.get(self.local_slot)

    def _local_flags(self, now):
        player = self._local_player()
        flags = player.flags(now) if player is not None else 0
        if self.map_open:
            flags |= protocol.FLAG_MAP_OPEN
        return flags

    def _sync_local_player(self, now):
        player = self._local_player()
        if player is None:
            return
        player.x = self.player_x
        player.y = self.player_y
        player.heading = self.player_heading
        player.render_x = self.player_x
        player.render_y = self.player_y
        player.render_heading = self.player_heading
        player.map_open = self.map_open
        player.last_seen = now

    def _update_movement(self, dt, now):
        player = self._local_player()
        if player is not None and player.dead:
            return
        turn = (1 if "JOYRIGHT" in self.held else 0) - (
            1 if "JOYLEFT" in self.held else 0
        )
        forward = (1 if "JOYUP" in self.held else 0) - (
            1 if "JOYDOWN" in self.held else 0
        )
        strafe = 0
        if "A" in self.held:
            strafe = turn
            turn = 0
        self.player_heading = engine.wrap_angle(
            self.player_heading + turn * 2.0 * dt
        )
        forward_speed = 2.0 if forward >= 0 else 1.4
        forward_amount = forward * forward_speed * dt
        strafe_amount = strafe * 1.6 * dt
        cosine = math.cos(self.player_heading)
        sine = math.sin(self.player_heading)
        dx = cosine * forward_amount - sine * strafe_amount
        dy = sine * forward_amount + cosine * strafe_amount
        self.player_x, self.player_y = engine.move_with_collision(
            self.player_x, self.player_y, dx, dy
        )
        self._sync_local_player(now)

    def _smooth_remote_players(self, dt):
        amount = min(1.0, REMOTE_SMOOTH_RATE * dt)
        for slot, player in self.players.items():
            if slot == self.local_slot:
                continue
            player.render_x += (player.x - player.render_x) * amount
            player.render_y += (player.y - player.render_y) * amount
            player.render_heading = engine.wrap_angle(
                player.render_heading
                + engine.angle_difference(player.heading, player.render_heading)
                * amount
            )

    def _fire(self, now):
        if self.map_open or self.countdown_ms > 0:
            return
        player = self._local_player()
        if player is not None and player.dead:
            return
        self.muzzle_ms = 120
        if self.is_host:
            self._resolve_shot(self.local_slot, self.player_heading, now)
        elif self.host_mac is not None:
            self._send(
                protocol.pack_fire(
                    self.session, self._next_sequence(), self.player_heading
                ),
                self.host_mac,
            )

    def _resolve_shot(self, shooter_slot, shot_heading, now):
        shooter = self.players.get(shooter_slot)
        if shooter is None or shooter.dead or shooter.map_open:
            return None
        if shooter.last_fire is not None and _ticks_diff(now, shooter.last_fire) < FIRE_COOLDOWN_MS:
            return None
        if abs(engine.angle_difference(shot_heading, shooter.heading)) > 0.55:
            return None
        shooter.last_fire = now
        targets = []
        for slot, player in self.players.items():
            if slot == shooter_slot or player.dead:
                continue
            if player.protected_until and _ticks_diff(player.protected_until, now) > 0:
                continue
            if slot != self.local_slot and _ticks_diff(now, player.last_seen) > SHOT_STALE_MS:
                continue
            targets.append((slot, player.x, player.y))
        victim_slot, _ = engine.first_visible_target(
            shooter.x, shooter.y, shot_heading, targets, 8.0
        )
        if victim_slot is None:
            return None
        victim = self.players[victim_slot]
        victim.health = max(0, victim.health - DAMAGE)
        self._send(
            protocol.pack_hit(
                self.session,
                self._next_sequence(),
                shooter_slot,
                victim_slot,
                victim.health,
            )
        )
        if victim_slot == self.local_slot:
            self.hit_ms = 180
        if victim.health == 0:
            victim.dead = True
            victim.deaths = min(255, victim.deaths + 1)
            victim.respawn_at = _ticks_add(now, RESPAWN_MS)
            shooter.kills = min(255, shooter.kills + 1)
            shooter.streak = min(255, shooter.streak + 1)
            if shooter.kills >= 10:
                self._end_match(shooter.slot)
        return victim_slot

    def _update_respawns(self, now):
        for player in self.players.values():
            if player.dead and _ticks_diff(now, player.respawn_at) >= 0:
                player.x, player.y, player.heading = engine.SPAWNS[
                    (player.slot + player.deaths) % len(engine.SPAWNS)
                ]
                player.health = 100
                player.dead = False
                player.streak = 0
                player.render_x = player.x
                player.render_y = player.y
                player.render_heading = player.heading
                player.protected_until = _ticks_add(now, SPAWN_PROTECTION_MS)
                player.respawn_at = 0
                if player.slot == self.local_slot:
                    self.player_x = player.x
                    self.player_y = player.y
                    self.player_heading = player.heading

    def _winner_at_timeout(self):
        if not self.players:
            return 255
        best = max(player.kills for player in self.players.values())
        leaders = [player.slot for player in self.players.values() if player.kills == best]
        return leaders[0] if len(leaders) == 1 else 255

    def _end_match(self, winner):
        if self.mode != MATCH:
            return
        self.winner = winner
        ending = protocol.pack_end(self.session, self._next_sequence(), winner)
        self._send_to_clients(ending, repeats=2)
        self.mode = RESULTS

    def _broadcast_beacon(self, now):
        phase = protocol.PHASE_LOBBY if self.mode == HOST_LOBBY else protocol.PHASE_MATCH
        self._send(
            protocol.pack_beacon(
                self.session,
                self._next_sequence(),
                phase,
                len(self.players),
                self._wifi_channel(),
                self.local_name,
            )
        )
        self.last_beacon = now

    def _broadcast_snapshot(self, now):
        snapshots = [self.players[slot].snapshot(now) for slot in sorted(self.players)]
        self._send(
            protocol.pack_snapshot(
                self.session, self._next_sequence(), self.remaining_ms, snapshots
            )
        )
        self.last_snapshot = now

    def _send_pose(self, now):
        self._send(
            protocol.pack_pose(
                self.session,
                self._next_sequence(),
                self.player_x,
                self.player_y,
                self.player_heading,
                self._local_flags(now),
            ),
            self.host_mac,
        )
        self.last_pose = now

    def _prune_hosts(self, now):
        for mac in tuple(self.hosts):
            if _ticks_diff(now, self.hosts[mac]["seen"]) > PEER_TIMEOUT_MS:
                del self.hosts[mac]
        if self.hosts:
            self.host_selection %= len(self.hosts)
        else:
            self.host_selection = 0

    def _prune_players(self, now):
        for slot, player in tuple(self.players.items()):
            if slot != self.local_slot and _ticks_diff(now, player.last_seen) > PEER_TIMEOUT_MS:
                self._remove_player(slot)
        if self.mode == MATCH and len(self.players) < 2:
            self._end_match(self.local_slot)

    def _handle_menu_input(self):
        if "JOYUP" in self.pressed or "JOYDOWN" in self.pressed:
            self.menu_index = 1 - self.menu_index
        if "C" in self.pressed:
            if self.menu_index == 0:
                self._start_host()
            else:
                self._start_browser()

    def _handle_lobby_input(self):
        if self.mode == HOST_LOBBY and "C" in self.pressed:
            self._begin_match()
        elif self.mode == JOIN_BROWSER:
            hosts = self._selected_hosts()
            if hosts:
                if "JOYUP" in self.pressed:
                    self.host_selection = (self.host_selection - 1) % len(hosts)
                if "JOYDOWN" in self.pressed:
                    self.host_selection = (self.host_selection + 1) % len(hosts)
            if "C" in self.pressed:
                self._join_selected()

    def update(self, delta):
        if self.mode != MENU:
            self._ensure_radio()
        now = _ticks_ms()
        if "F" in self.pressed:
            self.pressed.clear()
            self.minimise()
            return False

        dt_ms = max(0, min(100, int(delta)))
        dt = dt_ms * 0.001
        self.muzzle_ms = max(0, self.muzzle_ms - dt_ms)
        self.hit_ms = max(0, self.hit_ms - dt_ms)

        if self.mode == MENU:
            self._handle_menu_input()
        elif self.mode in (HOST_LOBBY, JOIN_BROWSER, CLIENT_LOBBY):
            self._handle_lobby_input()
        elif self.mode == MATCH:
            if "B" in self.pressed:
                self.map_open = not self.map_open
            if self.countdown_ms > 0:
                self.countdown_ms = max(0, self.countdown_ms - dt_ms)
            else:
                self._update_movement(dt, now)
                if "C" in self.pressed:
                    self._fire(now)
            self._smooth_remote_players(dt)
        elif self.mode == RESULTS and "C" in self.pressed:
            self._reset_to_menu("MATCH COMPLETE")

        if self.mode == HOST_LOBBY and _ticks_diff(now, self.last_beacon) >= BEACON_INTERVAL_MS:
            self._broadcast_beacon(now)
        elif self.mode == JOIN_BROWSER:
            self._prune_hosts(now)
        elif self.mode == CLIENT_LOBBY:
            if _ticks_diff(now, self.last_host_seen) > PEER_TIMEOUT_MS:
                self._reset_to_menu("HOST TIMEOUT")
            elif _ticks_diff(now, self.last_pose) >= 1000:
                self._send(
                    protocol.pack_join(
                        self.session, self._next_sequence(), self.local_name
                    ),
                    self.host_mac,
                )
                self.last_pose = now
        elif self.mode == MATCH:
            if self.is_host:
                self._sync_local_player(now)
                self._prune_players(now)
                self._update_respawns(now)
                if self.countdown_ms <= 0:
                    self.remaining_ms = max(0, self.remaining_ms - dt_ms)
                    if self.remaining_ms == 0:
                        self._end_match(self._winner_at_timeout())
                if self.mode == MATCH and _ticks_diff(now, self.last_snapshot) >= SNAPSHOT_INTERVAL_MS:
                    self._broadcast_snapshot(now)
            else:
                if _ticks_diff(now, self.last_host_seen) > PEER_TIMEOUT_MS:
                    self._reset_to_menu("HOST TIMEOUT")
                elif _ticks_diff(now, self.last_pose) >= POSE_INTERVAL_MS:
                    self._send_pose(now)

        self.pressed.clear()
        return True

    @staticmethod
    def _setup_text(ctx, size=11):
        ctx.font = ctx.get_font_name(0)
        ctx.font_size = size
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

    def _draw_menu(self, ctx):
        ctx.rgb(0.015, 0.025, 0.05).rectangle(-120, -120, 240, 240).fill()
        self._setup_text(ctx, 24)
        ctx.rgb(0.20, 0.92, 1.00).move_to(0, -72).text("BADGEBLUD")
        self._setup_text(ctx, 13)
        options = ("HOST MATCH", "JOIN MATCH")
        for index, label in enumerate(options):
            colour = (1.0, 0.24, 0.62) if index == self.menu_index else (0.55, 0.62, 0.72)
            ctx.rgb(*colour).move_to(0, -15 + index * 34).text(
                ("> " if index == self.menu_index else "  ") + label
            )
        self._setup_text(ctx, 9)
        ctx.rgb(0.65, 0.72, 0.82).move_to(0, 74).text(self.status[:28])
        ctx.move_to(0, 96).text("C SELECTS / FIRES   F EXIT")

    def _draw_lobby(self, ctx):
        ctx.rgb(0.015, 0.025, 0.05).rectangle(-120, -120, 240, 240).fill()
        self._setup_text(ctx, 18)
        title = "HOST LOBBY" if self.mode == HOST_LOBBY else "FIND MATCH"
        if self.mode == CLIENT_LOBBY:
            title = "JOINED"
        ctx.rgb(0.20, 0.92, 1.00).move_to(0, -92).text(title)
        self._setup_text(ctx, 10)
        ctx.rgb(0.65, 0.72, 0.82).move_to(0, -70).text(
            "CHANNEL %s" % (self._wifi_channel() or "?")
        )
        if self.mode == HOST_LOBBY:
            for index, slot in enumerate(sorted(self.players)):
                player = self.players[slot]
                ctx.rgb(*PLAYER_COLOURS[slot]).move_to(0, -38 + index * 25).text(
                    "P%d  %s" % (slot + 1, player.name)
                )
            ctx.rgb(0.75, 0.80, 0.88).move_to(0, 82).text("C STARTS AT 2+")
        elif self.mode == JOIN_BROWSER:
            hosts = self._selected_hosts()
            if not hosts:
                ctx.rgb(0.75, 0.80, 0.88).move_to(0, -5).text("SCANNING...")
            for index, (_, details) in enumerate(hosts[:4]):
                selected = index == self.host_selection
                ctx.rgb(*(PLAYER_COLOURS[1] if selected else (0.60, 0.66, 0.75))).move_to(
                    0, -38 + index * 27
                ).text(
                    ("> " if selected else "  ")
                    + "%s %d/4 CH%d"
                    % (details["name"], details["count"], details["channel"])
                )
        else:
            ctx.rgb(*PLAYER_COLOURS[self.local_slot]).move_to(0, -5).text(self.status)
            ctx.rgb(0.75, 0.80, 0.88).move_to(0, 30).text("WAITING FOR HOST")
        ctx.rgb(0.65, 0.72, 0.82).move_to(0, 104).text(self.status[:28])

    def _visible_players(self):
        visible = []
        sine = math.sin(self.player_heading)
        cosine = math.cos(self.player_heading)
        focal = 120.0 / math.tan(engine.FOV * 0.5)
        for slot, player in self.players.items():
            if slot == self.local_slot or player.dead:
                continue
            dx = player.render_x - self.player_x
            dy = player.render_y - self.player_y
            forward = dx * cosine + dy * sine
            lateral = -dx * sine + dy * cosine
            if forward <= 0.15:
                continue
            screen_x = lateral * focal / forward
            if screen_x < -132 or screen_x > 132:
                continue
            column = int((screen_x + 120.0) * self.raycaster.count / 240.0)
            column = int(engine.clamp(column, 0, self.raycaster.count - 1))
            if forward >= self.raycaster.depths[column] + 0.20:
                continue
            visible.append((forward, screen_x, slot, player))
        visible.sort(reverse=True)
        return visible

    def _draw_world(self, ctx):
        ctx.rgb(0.025, 0.045, 0.09).rectangle(-120, -120, 240, 120).fill()
        ctx.rgb(0.025, 0.025, 0.035).rectangle(-120, 0, 240, 120).fill()
        depths, shades = self.raycaster.cast(
            self.player_x, self.player_y, self.player_heading
        )
        column_width = 240.0 / self.raycaster.count
        for shade, colour in enumerate(WALL_COLOURS):
            ctx.rgb(*colour).begin_path()
            for index in range(self.raycaster.count):
                if shades[index] == shade:
                    height = engine.clamp(150.0 / depths[index], 8.0, 240.0)
                    ctx.rectangle(
                        -120.0 + index * column_width,
                        -height * 0.5,
                        column_width + 0.5,
                        height,
                    )
            ctx.fill()

        for forward, screen_x, slot, _ in self._visible_players():
            height = engine.clamp(72.0 / forward, 5.0, 72.0)
            width = height * 0.42
            ctx.rgb(*PLAYER_COLOURS[slot]).rectangle(
                screen_x - width * 0.5, -height * 0.45, width, height
            ).fill()

        ctx.rgb(0.92, 0.96, 1.0).begin_path()
        ctx.move_to(-6, 0).line_to(-2, 0)
        ctx.move_to(2, 0).line_to(6, 0)
        ctx.move_to(0, -6).line_to(0, -2)
        ctx.move_to(0, 2).line_to(0, 6)
        ctx.stroke()
        if self.muzzle_ms:
            ctx.rgb(1.0, 0.72, 0.12).arc(0, 20, 5, 0, engine.TAU, False).fill()
        if self.hit_ms:
            ctx.rgba(1.0, 0.05, 0.08, 0.32).rectangle(-120, -120, 240, 240).fill()

    def _draw_map(self, ctx):
        ctx.rgb(0.015, 0.025, 0.05).rectangle(-120, -120, 240, 240).fill()
        scale = 14.0
        origin_x = -engine.MAP_WIDTH * scale * 0.5
        origin_y = -engine.MAP_HEIGHT * scale * 0.5
        ctx.rgb(0.08, 0.24, 0.34).begin_path()
        for start, row, length in engine.WALL_RUNS:
            ctx.rectangle(
                origin_x + start * scale,
                origin_y + row * scale,
                length * scale,
                scale,
            )
        ctx.fill()
        for slot, player in self.players.items():
            x = origin_x + player.render_x * scale
            y = origin_y + player.render_y * scale
            size = 6.5 if slot == self.local_slot else 5.0
            cosine = math.cos(player.render_heading)
            sine = math.sin(player.render_heading)
            ctx.rgb(*PLAYER_COLOURS[slot]).begin_path()
            ctx.move_to(x + cosine * size, y + sine * size)
            ctx.line_to(x + math.cos(player.render_heading + 2.45) * size, y + math.sin(player.render_heading + 2.45) * size)
            ctx.line_to(x + math.cos(player.render_heading - 2.45) * size, y + math.sin(player.render_heading - 2.45) * size)
            ctx.close_path().fill()

    @staticmethod
    def _format_time(milliseconds):
        seconds = max(0, milliseconds) // 1000
        return "%d:%02d" % (seconds // 60, seconds % 60)

    def _draw_match_hud(self, ctx):
        player = self._local_player()
        health = player.health if player is not None else 0
        kills = player.kills if player is not None else 0
        self._setup_text(ctx, 10)
        ctx.rgb(0.96, 0.98, 1.0).move_to(0, -108).text(
            "HP %03d   K %02d   %s" % (health, kills, self._format_time(self.remaining_ms))
        )
        ctx.move_to(0, 109).text("C FIRE   B MAP   A+NUB STRAFE")
        if self.countdown_ms > 0:
            self._setup_text(ctx, 30)
            ctx.rgb(1.0, 0.76, 0.12).move_to(0, 0).text(
                str(max(1, int(math.ceil(self.countdown_ms / 1000.0))))
            )
        elif player is not None and player.dead:
            self._setup_text(ctx, 15)
            ctx.rgb(1.0, 0.24, 0.32).move_to(0, -8).text("ELIMINATED")
            self._setup_text(ctx, 11)
            ctx.move_to(0, 12).text("LIFE STREAK %d" % player.streak)

    def _draw_results(self, ctx):
        ctx.rgb(0.015, 0.025, 0.05).rectangle(-120, -120, 240, 240).fill()
        self._setup_text(ctx, 22)
        label = "DRAW" if self.winner == 255 else "P%d WINS" % (self.winner + 1)
        if self.winner == self.local_slot:
            label = "YOU WIN"
        ctx.rgb(1.0, 0.76, 0.12).move_to(0, -75).text(label)
        self._setup_text(ctx, 11)
        for index, player in enumerate(
            sorted(self.players.values(), key=lambda item: (-item.kills, item.slot))
        ):
            ctx.rgb(*PLAYER_COLOURS[player.slot]).move_to(0, -30 + index * 25).text(
                "%s  %dK/%dD" % (player.name, player.kills, player.deaths)
            )
        ctx.rgb(0.75, 0.80, 0.88).move_to(0, 92).text("C RETURNS")

    def draw(self, ctx):
        ctx.save()
        if self.mode == MENU:
            self._draw_menu(ctx)
        elif self.mode in (HOST_LOBBY, JOIN_BROWSER, CLIENT_LOBBY):
            self._draw_lobby(ctx)
        elif self.mode == MATCH:
            if self.map_open:
                self._draw_map(ctx)
            else:
                self._draw_world(ctx)
            self._draw_match_hud(ctx)
        else:
            self._draw_results(ctx)
        ctx.restore()


__app_export__ = BadgeBludApp
