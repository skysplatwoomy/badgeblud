# BadgeBlud

BadgeBlud is a 2–4 player ray-cast deathmatch game for EMF 2026 Tildagon
badges. One badge hosts and the others join over the firmware's shared ESP-NOW
service. The app temporarily aligns participating radios to channel 6 and
restores the previous channel when it exits.

## Controls

- Nub up/down: move forward/backward.
- Nub left/right: turn quickly.
- Hold A with nub left/right: strafe.
- Swipe clockwise/counter-clockwise across adjacent touch pads: fine aim.
- C: confirm menu choices, start/leave matches, and fire during gameplay.
- B: toggle the live tactical map. Movement remains active; firing is disabled.
- F: leave the session and exit.

Hits are one-shot eliminations. The death screen shows the kill streak from the
life that just ended. The first player to ten eliminations wins; if nobody
reaches ten within three minutes, the highest score wins.

## Host tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 badgeblud/tests/test_badgeblud.py
```

Only `__init__.py`, `app.py`, `engine.py`, `main.py`, `metadata.json`, and
`protocol.py` belong on the badge. `tests/` and this README are host-only.
