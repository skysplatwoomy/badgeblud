"""Host-side behavioral tests for BadgeBlud."""

import math
from pathlib import Path
import sys
import types
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeBaseApp:
    def __init__(self):
        self.overlays = []
        self.minimised = False

    def minimise(self):
        self.minimised = True


class ButtonDownEvent:
    pass


class ButtonUpEvent:
    pass


class EspNowReceiveEvent:
    def __init__(self, mac, msg, rssi=-42, timestamp=0):
        self.mac = mac
        self.msg = msg
        self.rssi = rssi
        self.timestamp = timestamp


class FakeButton:
    def __init__(self, name):
        self.name = name


class FakeButtonEvent:
    def __init__(self, name):
        self.button = FakeButton(name)


class FakeEventBus:
    def __init__(self):
        self.handlers = []
        self.removed = []

    def on(self, event_type, handler, owner):
        self.handlers.append((event_type, handler, owner))

    def remove(self, event_type, handler, owner):
        self.removed.append((event_type, handler, owner))


class FakeEspNowService:
    def __init__(self):
        self.wifi_channel = 6
        self.sent = []
        self.subscriptions = []

    def subscribe(self, handler, owner, predicate=None):
        self.subscriptions.append((handler, owner, predicate))
        return handler

    def send(self, data, mac=b"\xff" * 6):
        self.sent.append((data, mac))
        return True


class FakeWLAN:
    def __init__(self):
        self.channel = 13
        self.connected = False
        self.disconnect_count = 0
        self.channel_failures = 0

    def active(self, value=None):
        return True

    def config(self, key=None, **values):
        if "channel" in values:
            if self.channel_failures:
                self.channel_failures -= 1
                raise OSError("WiFi Unknown Error")
            self.channel = values["channel"]
            return None
        if key == "mac":
            return b"\x10\x20\x30\x40\x50\x60"
        if key == "channel":
            return self.channel
        raise KeyError(key)

    def isconnected(self):
        return self.connected

    def disconnect(self):
        self.disconnect_count += 1
        self.connected = False


fake_wlan = FakeWLAN()


class FakeWifiModule(types.ModuleType):
    def __init__(self):
        super().__init__("wifi")
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        fake_wlan.connected = True


fake_wifi = FakeWifiModule()


fake_eventbus = FakeEventBus()
fake_espnow = FakeEspNowService()


def install_stubs():
    app_module = types.ModuleType("app")
    app_module.App = FakeBaseApp
    sys.modules["app"] = app_module

    events_package = types.ModuleType("events")
    input_module = types.ModuleType("events.input")
    input_module.ButtonDownEvent = ButtonDownEvent
    input_module.ButtonUpEvent = ButtonUpEvent
    sys.modules["events"] = events_package
    sys.modules["events.input"] = input_module

    system_package = types.ModuleType("system")
    eventbus_module = types.ModuleType("system.eventbus")
    eventbus_module.eventbus = fake_eventbus
    espnow_module = types.ModuleType("system.espnow")
    espnow_module.BROADCAST_MAC = b"\xff" * 6
    espnow_module.EspNowReceiveEvent = EspNowReceiveEvent
    espnow_module.espnow_service = fake_espnow
    sys.modules["system"] = system_package
    sys.modules["system.eventbus"] = eventbus_module
    sys.modules["system.espnow"] = espnow_module

    settings_module = types.ModuleType("settings")
    settings_module.get = lambda key, default=None: "TestBadge"
    sys.modules["settings"] = settings_module

    network_module = types.ModuleType("network")
    network_module.STA_IF = 0
    network_module.WLAN = lambda interface: fake_wlan
    sys.modules["network"] = network_module
    sys.modules["wifi"] = fake_wifi


install_stubs()

from badgeblud import app as game
from badgeblud import engine, protocol


class FakeClock:
    def __init__(self):
        self.now = 10000

    def advance(self, amount):
        self.now += amount


clock = FakeClock()
game._ticks_ms = lambda: clock.now
game._ticks_diff = lambda left, right: left - right
game._ticks_add = lambda value, delta: value + delta


class FakeCtx:
    CENTER = "center"
    MIDDLE = "middle"

    def __init__(self):
        self.operations = []
        self.depth = 0
        self.font = None
        self.font_size = 0
        self.text_align = None
        self.text_baseline = None

    def _chain(self, name, *values):
        self.operations.append((name, values))
        return self

    def save(self):
        self.depth += 1
        return self._chain("save")

    def restore(self):
        self.depth -= 1
        return self._chain("restore")

    def get_font_name(self, index):
        return "fake-font"

    def rgb(self, *values):
        return self._chain("rgb", *values)

    def rgba(self, *values):
        return self._chain("rgba", *values)

    def rectangle(self, *values):
        return self._chain("rectangle", *values)

    def arc(self, *values):
        return self._chain("arc", *values)

    def begin_path(self):
        return self._chain("begin_path")

    def move_to(self, *values):
        return self._chain("move_to", *values)

    def line_to(self, *values):
        return self._chain("line_to", *values)

    def close_path(self):
        return self._chain("close_path")

    def fill(self):
        return self._chain("fill")

    def stroke(self):
        return self._chain("stroke")

    def text(self, value):
        return self._chain("text", value)


class EngineTests(unittest.TestCase):
    def test_arena_and_spawns_are_valid(self):
        self.assertTrue(all(len(row) == engine.MAP_WIDTH for row in engine.ARENA))
        for x, y, _ in engine.SPAWNS:
            self.assertTrue(engine.position_is_open(x, y))

    def test_every_spawn_pair_is_separated_by_a_wall(self):
        for source_index, source in enumerate(engine.SPAWNS):
            for target_index, target in enumerate(engine.SPAWNS):
                if source_index >= target_index:
                    continue
                heading = math.atan2(target[1] - source[1], target[0] - source[0])
                visible, _ = engine.first_visible_target(
                    source[0],
                    source[1],
                    heading,
                    ((target_index, target[0], target[1]),),
                    engine.MAX_RAY_DISTANCE,
                )
                self.assertIsNone(
                    visible,
                    "spawn %d can see spawn %d" % (source_index, target_index),
                )

    def test_cardinal_rays_and_wall_collision(self):
        west, _ = engine.cast_ray(1.5, 1.5, math.pi)
        east, _ = engine.cast_ray(1.5, 1.5, 0.0)
        self.assertAlmostEqual(west, 0.5, places=4)
        self.assertAlmostEqual(east, 4.5, places=4)
        x, y = engine.move_with_collision(1.5, 1.5, -1.0, 0.25)
        self.assertAlmostEqual(x, 1.5)
        self.assertAlmostEqual(y, 1.75)

    def test_nearest_visible_target_stops_at_wall(self):
        target, distance = engine.first_visible_target(
            1.5, 1.5, 0.0, ((1, 4.5, 1.5), (2, 6.5, 1.5))
        )
        self.assertEqual(target, 1)
        self.assertAlmostEqual(distance, 3.0)
        blocked, _ = engine.first_visible_target(
            1.5, 2.5, 0.0, ((2, 3.5, 2.5),)
        )
        self.assertIsNone(blocked)

    def test_raycaster_preallocates_and_quantizes(self):
        caster = engine.RayCaster()
        depth_identity = id(caster.depths)
        depths, shades = caster.cast(1.5, 1.5, 0.0)
        self.assertEqual(id(depths), depth_identity)
        self.assertEqual(len(depths), 40)
        self.assertTrue(all(0 <= shade <= 5 for shade in shades))


class ProtocolTests(unittest.TestCase):
    def round_trip(self, packed, unpacker):
        message_type, session, sequence, payload = protocol.parse(packed)
        self.assertEqual(session, 37)
        self.assertEqual(sequence, 9)
        return message_type, unpacker(payload)

    def test_small_packets_round_trip(self):
        kind, beacon = self.round_trip(
            protocol.pack_beacon(37, 9, 0, 3, 6, "LongerBadgeName"),
            protocol.unpack_beacon,
        )
        self.assertEqual(kind, protocol.BEACON)
        self.assertEqual(beacon, (0, 3, 6, "LongerBa"))
        self.assertEqual(
            self.round_trip(protocol.pack_join(37, 9, "alice"), protocol.unpack_join)[1],
            "alice",
        )
        self.assertEqual(
            self.round_trip(protocol.pack_accept(37, 9, 2), protocol.unpack_accept)[1],
            2,
        )
        self.assertEqual(
            self.round_trip(protocol.pack_reject(37, 9, 1), protocol.unpack_reject)[1],
            1,
        )
        self.assertEqual(
            self.round_trip(protocol.pack_start(37, 9, 3000), protocol.unpack_start)[1],
            3000,
        )
        _, pose = self.round_trip(
            protocol.pack_pose(37, 9, 4.25, 6.5, -0.4, protocol.FLAG_MAP_OPEN),
            protocol.unpack_pose,
        )
        self.assertAlmostEqual(pose[0], 4.25, places=2)
        self.assertAlmostEqual(engine.angle_difference(pose[2], -0.4), 0.0, places=3)
        self.assertEqual(pose[3], protocol.FLAG_MAP_OPEN)
        self.assertEqual(
            self.round_trip(protocol.pack_hit(37, 9, 1, 2, 66), protocol.unpack_hit)[1],
            (1, 2, 66),
        )
        self.assertEqual(
            self.round_trip(protocol.pack_end(37, 9, 255), protocol.unpack_end)[1],
            255,
        )

    def test_names_use_fixed_ascii_without_cpython_bytes_helpers(self):
        self.assertEqual(protocol.clean_name("bädge"), b"bdge    ")
        self.assertEqual(protocol.decode_name(b"ok \xff \x00"), "ok?")
        self.assertEqual(protocol.decode_name(b"        "), "badge")

    def test_four_player_snapshot_stays_small_and_round_trips(self):
        players = []
        for slot in range(4):
            players.append(
                {
                    "slot": slot,
                    "mac": bytes((slot,)) * 6,
                    "name": "player%d" % slot,
                    "x": 1.5 + slot,
                    "y": 2.5 + slot,
                    "heading": slot * 0.5,
                    "health": 100 - slot,
                    "kills": slot,
                    "deaths": slot + 1,
                    "streak": slot + 2,
                    "flags": slot,
                    "respawn_ms": slot * 100,
                }
            )
        packed = protocol.pack_snapshot(37, 9, 180000, players)
        self.assertLess(len(packed), 250)
        _, _, _, payload = protocol.parse(packed)
        remaining, decoded = protocol.unpack_snapshot(payload)
        self.assertEqual(remaining, 180000)
        self.assertEqual(len(decoded), 4)
        self.assertEqual(decoded[3]["slot"], 3)
        self.assertEqual(decoded[3]["kills"], 3)
        self.assertEqual(decoded[3]["streak"], 5)

    def test_malformed_and_foreign_packets_are_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse(b"")
        good = bytearray(protocol.pack_leave(1, 1))
        good[0:2] = b"XX"
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse(good)
        packet = protocol.pack_snapshot(1, 1, 10, []) + b"junk"
        _, _, _, payload = protocol.parse(packet)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_snapshot(payload)

    def test_sequence_wrap_distinguishes_new_and_duplicate_packets(self):
        self.assertTrue(game._sequence_is_new(0, 65535))
        self.assertFalse(game._sequence_is_new(25, 25))
        self.assertFalse(game._sequence_is_new(65000, 25))


class AppTests(unittest.TestCase):
    def setUp(self):
        clock.now = 10000
        fake_eventbus.handlers.clear()
        fake_eventbus.removed.clear()
        fake_espnow.sent.clear()
        fake_espnow.subscriptions.clear()
        fake_wlan.channel = 13
        fake_wlan.connected = False
        fake_wlan.disconnect_count = 0
        fake_wlan.channel_failures = 0
        fake_wifi.connect_count = 0
        self.app = game.BadgeBludApp()

    def press(self, name):
        self.app._button_down(FakeButtonEvent(name))

    def release(self, name):
        self.app._button_up(FakeButtonEvent(name))

    def make_host_match(self, opponents=1):
        self.app._start_host()
        for slot in range(1, opponents + 1):
            mac = bytes((slot,)) * 6
            player = game.Player(slot, mac, "peer%d" % slot, clock.now)
            self.app.players[slot] = player
            self.app.mac_to_slot[mac] = slot
        self.app._begin_match()
        self.app.countdown_ms = 0
        self.app.player_x = 1.5
        self.app.player_y = 1.5
        self.app.player_heading = 0.0
        self.app._sync_local_player(clock.now)
        for player in self.app.players.values():
            player.protected_until = 0

    def test_menu_uses_physical_names_and_b_does_not_select(self):
        self.press("B")
        self.app.update(50)
        self.assertEqual(self.app.mode, game.MENU)
        self.assertEqual(self.app.menu_index, 0)
        self.release("B")
        self.press("JOYDOWN")
        self.app.update(50)
        self.assertEqual(self.app.menu_index, 1)
        self.release("JOYDOWN")
        self.press("JOYFIRE")
        self.app.update(50)
        self.assertEqual(self.app.mode, game.MENU)
        self.release("JOYFIRE")
        self.press("C")
        self.app.update(50)
        self.assertEqual(self.app.mode, game.JOIN_BROWSER)

    def test_client_lobby_beacons_keep_joiner_alive(self):
        host_mac = b"\xaa" * 6
        self.app.mode = game.CLIENT_LOBBY
        self.app.session = 77
        self.app.host_mac = host_mac
        self.app.last_host_seen = clock.now
        for sequence in range(1, 5):
            clock.advance(2000)
            beacon = protocol.pack_beacon(
                77, sequence, protocol.PHASE_LOBBY, 2, 6, "host"
            )
            self.app._on_radio(EspNowReceiveEvent(host_mac, beacon))
            self.app.update(50)
            self.assertEqual(self.app.mode, game.CLIENT_LOBBY)
            self.assertEqual(self.app.last_host_seen, clock.now)

    def test_session_aligns_radio_channel_and_restores_previous_wifi(self):
        fake_wlan.channel = 13
        fake_wlan.connected = True
        self.app._start_host()
        self.assertEqual(self.app.mode, game.HOST_LOBBY)
        self.assertEqual(fake_wlan.channel, game.GAME_CHANNEL)
        self.assertFalse(fake_wlan.connected)
        self.app._reset_to_menu()
        self.assertEqual(fake_wlan.channel, 13)
        self.assertTrue(fake_wlan.connected)
        self.assertEqual(fake_wifi.connect_count, 1)

    def test_channel_claim_cancels_pending_wifi_and_retries_driver_errors(self):
        fake_wlan.connected = False
        fake_wlan.channel = 1
        fake_wlan.channel_failures = 2
        self.app._start_host()
        self.assertEqual(self.app.mode, game.HOST_LOBBY)
        self.assertEqual(fake_wlan.channel, game.GAME_CHANNEL)
        self.assertGreaterEqual(fake_wlan.disconnect_count, 3)

    def test_c_starts_host_match_but_nub_does_not(self):
        self.app._start_host()
        peer = game.Player(1, b"\x01" * 6, "peer", clock.now)
        self.app.players[1] = peer
        self.app.mac_to_slot[peer.mac] = 1
        self.press("JOYFIRE")
        self.app.update(50)
        self.assertEqual(self.app.mode, game.HOST_LOBBY)
        self.release("JOYFIRE")
        self.press("C")
        self.app.update(50)
        self.assertEqual(self.app.mode, game.MATCH)

    def test_touch_wrap_and_timeout(self):
        self.make_host_match()
        self.app.player_heading = 0.0
        self.press("TOUCH12")
        self.release("TOUCH12")
        clock.advance(150)
        self.press("TOUCH01")
        self.assertAlmostEqual(self.app.player_heading, 0.045)
        self.release("TOUCH01")
        clock.advance(150)
        self.press("TOUCH12")
        self.assertAlmostEqual(self.app.player_heading, 0.0)
        self.release("TOUCH12")
        clock.advance(451)
        self.press("TOUCH01")
        self.assertAlmostEqual(self.app.player_heading, 0.0)

    def test_turn_and_a_modifier_strafe(self):
        self.make_host_match()
        self.press("JOYRIGHT")
        self.app.update(100)
        self.assertAlmostEqual(self.app.player_heading, 0.2)
        self.release("JOYRIGHT")
        self.app.player_heading = 0.0
        self.press("A")
        self.press("JOYRIGHT")
        before = (self.app.player_x, self.app.player_y)
        self.app.update(100)
        self.assertAlmostEqual(self.app.player_heading, 0.0)
        self.assertAlmostEqual(self.app.player_x, before[0])
        self.assertGreater(self.app.player_y, before[1])

    def test_map_toggle_keeps_movement_and_blocks_fire(self):
        self.make_host_match()
        opponent = self.app.players[1]
        opponent.x, opponent.y, opponent.heading = 4.5, 1.5, math.pi
        self.press("B")
        self.press("JOYUP")
        self.app.update(100)
        self.assertTrue(self.app.map_open)
        self.assertGreater(self.app.player_x, 1.5)
        self.release("B")
        self.release("JOYUP")
        self.press("C")
        self.app.update(50)
        self.assertEqual(opponent.health, 100)
        self.release("C")
        self.press("B")
        self.app.update(50)
        self.release("B")
        self.press("JOYFIRE")
        self.app.update(50)
        self.assertEqual(opponent.health, 100)
        self.release("JOYFIRE")
        self.press("C")
        self.app.update(50)
        self.assertEqual(opponent.health, 0)
        self.assertTrue(opponent.dead)

    def test_one_hit_kills_and_streak_survives_until_respawn(self):
        self.make_host_match()
        opponent = self.app.players[1]
        opponent.x, opponent.y = 4.5, 1.5
        opponent.streak = 3
        self.assertEqual(
            self.app._resolve_shot(0, 0.0, clock.now), opponent.slot
        )
        self.assertEqual(opponent.health, 0)
        self.assertTrue(opponent.dead)
        self.assertEqual(self.app.players[0].kills, 1)
        self.assertEqual(self.app.players[0].streak, 1)
        self.assertEqual(opponent.streak, 3)

        ctx = FakeCtx()
        self.app.local_slot = 1
        self.app.draw(ctx)
        texts = [values[0] for name, values in ctx.operations if name == "text"]
        self.assertIn("LIFE STREAK 3", texts)

        clock.advance(game.RESPAWN_MS)
        self.app._update_respawns(clock.now)
        self.assertFalse(opponent.dead)
        self.assertEqual(opponent.health, 100)
        self.assertEqual(opponent.streak, 0)
        self.assertNotEqual((opponent.x, opponent.y), (4.5, 1.5))

    def test_host_join_capacity_and_late_join(self):
        self.app._start_host()
        for slot in range(1, 4):
            mac = bytes((slot,)) * 6
            message = protocol.pack_join(
                self.app.session, slot, "peer%d" % slot
            )
            self.app._on_radio(EspNowReceiveEvent(mac, message))
        self.assertEqual(len(self.app.players), 4)
        full_mac = b"\x09" * 6
        self.app._on_radio(
            EspNowReceiveEvent(
                full_mac,
                protocol.pack_join(self.app.session, 9, "full"),
            )
        )
        kind, _, _, payload = protocol.parse(fake_espnow.sent[-1][0])
        self.assertEqual(kind, protocol.REJECT)
        self.assertEqual(protocol.unpack_reject(payload), protocol.REJECT_FULL)
        self.app._begin_match()
        late_mac = b"\x08" * 6
        self.app._on_radio(
            EspNowReceiveEvent(
                late_mac,
                protocol.pack_join(self.app.session, 10, "late"),
            )
        )
        kind, _, _, payload = protocol.parse(fake_espnow.sent[-1][0])
        self.assertEqual(kind, protocol.REJECT)
        self.assertEqual(protocol.unpack_reject(payload), protocol.REJECT_STARTED)

    def test_snapshot_places_all_players_on_client_map(self):
        host_mac = b"\xaa" * 6
        self.app.mode = game.CLIENT_LOBBY
        self.app.session = 77
        self.app.host_mac = host_mac
        self.app.local_slot = 1
        players = []
        for slot in range(4):
            player = game.Player(slot, bytes((slot,)) * 6, "p%d" % slot, clock.now)
            player.x = 1.5 + slot * 2
            players.append(player.snapshot(clock.now))
        message = protocol.pack_snapshot(77, 1, 170000, players)
        self.app._on_radio(EspNowReceiveEvent(host_mac, message))
        self.assertEqual(self.app.mode, game.MATCH)
        self.assertEqual(len(self.app.players), 4)
        self.assertEqual(self.app.remaining_ms, 170000)
        self.app.map_open = True
        ctx = FakeCtx()
        self.app.draw(ctx)
        triangle_count = sum(1 for name, _ in ctx.operations if name == "close_path")
        self.assertEqual(triangle_count, 4)

    def test_client_pose_is_relayed_without_host_validation_at_five_hz(self):
        self.make_host_match()
        remote = self.app.players[1]
        payload = protocol.pack_pose(
            self.app.session, 40, -20.0, 30.0, 2.25, 0
        )
        self.app._on_radio(EspNowReceiveEvent(remote.mac, payload))
        self.assertAlmostEqual(remote.x, -20.0)
        self.assertAlmostEqual(remote.y, 30.0)
        self.assertAlmostEqual(remote.heading, 2.25, places=3)

        self.app._broadcast_snapshot(clock.now)
        kind, _, _, snapshot_payload = protocol.parse(fake_espnow.sent[-1][0])
        self.assertEqual(kind, protocol.SNAPSHOT)
        _, players = protocol.unpack_snapshot(snapshot_payload)
        relayed = next(player for player in players if player["slot"] == 1)
        self.assertAlmostEqual(relayed["x"], -20.0)
        self.assertAlmostEqual(relayed["y"], 30.0)

    def test_game_radio_filters_foreign_espnow_traffic(self):
        self.app._start_host()
        _, _, predicate = fake_espnow.subscriptions[-1]
        self.assertTrue(predicate(EspNowReceiveEvent(b"\x01" * 6, b"BBpayload")))
        self.assertFalse(predicate(EspNowReceiveEvent(b"\x01" * 6, b"other-app")))
        self.assertEqual(game.POSE_INTERVAL_MS, 200)
        self.assertEqual(game.SNAPSHOT_INTERVAL_MS, 200)

    def test_host_does_not_validate_shot_aim_against_last_pose(self):
        self.make_host_match()
        shooter = self.app.players[1]
        shooter.x = 4.5
        shooter.y = 1.5
        shooter.heading = 0.0
        self.app.players[0].x = 1.5
        self.app.players[0].y = 1.5
        self.assertEqual(
            self.app._resolve_shot(1, math.pi, clock.now),
            0,
        )

    def test_duplicate_fire_and_stale_peer_are_rejected(self):
        self.make_host_match()
        peer = self.app.players[1]
        peer.x, peer.y, peer.heading = 4.5, 1.5, math.pi
        peer.last_seen = clock.now - game.SHOT_STALE_MS - 1
        self.assertIsNone(self.app._resolve_shot(0, 0.0, clock.now))
        peer.last_seen = clock.now
        fire = protocol.pack_fire(self.app.session, 44, math.pi)
        self.app._on_radio(EspNowReceiveEvent(peer.mac, fire))
        first_fire = peer.last_fire
        self.app._on_radio(EspNowReceiveEvent(peer.mac, fire))
        self.assertEqual(peer.last_fire, first_fire)

    def test_peer_and_host_timeouts(self):
        self.make_host_match()
        self.app.players[1].last_seen = clock.now - game.PEER_TIMEOUT_MS - 1
        self.app._prune_players(clock.now)
        self.assertNotIn(1, self.app.players)
        self.assertEqual(self.app.mode, game.RESULTS)

        client = game.BadgeBludApp()
        client.mode = game.CLIENT_LOBBY
        client.session = 88
        client.host_mac = b"\xaa" * 6
        client.last_host_seen = clock.now - game.PEER_TIMEOUT_MS - 1
        client.update(50)
        self.assertEqual(client.mode, game.MENU)
        self.assertEqual(client.status, "HOST TIMEOUT")

    def test_minimise_cleans_inputs_and_radio_subscription(self):
        self.app._start_host()
        self.press("JOYUP")
        self.press("TOUCH03")
        handler = self.app.radio_handler
        self.app.minimise()
        self.assertTrue(self.app.minimised)
        self.assertFalse(self.app.held)
        self.assertFalse(self.app.touch_held)
        self.assertIsNone(self.app.radio_handler)
        self.assertIn((EspNowReceiveEvent, handler, self.app), fake_eventbus.removed)
        self.app.update(50)
        self.assertIsNone(self.app.radio_handler)
        self.app._start_host()
        self.assertIsNotNone(self.app.radio_handler)

    def test_world_and_map_draws_are_balanced_and_within_budget(self):
        self.make_host_match(opponents=3)
        positions = ((1.5, 1.5), (3.5, 1.5), (5.5, 1.5), (8.5, 1.5))
        for slot, position in enumerate(positions):
            self.app.players[slot].x, self.app.players[slot].y = position
        self.app.player_x, self.app.player_y, self.app.player_heading = 1.5, 1.5, 0.0
        for map_open in (False, True):
            self.app.map_open = map_open
            ctx = FakeCtx()
            self.app.draw(ctx)
            self.assertEqual(ctx.depth, 0)
            self.assertLessEqual(
                len(ctx.operations),
                100,
                "map=%s used %d operations" % (map_open, len(ctx.operations)),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
