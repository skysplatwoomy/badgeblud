"""Pure geometry and ray-casting helpers for BadgeBlud."""

import math


TAU = 2.0 * math.pi
FOV = math.pi / 3.0
RAY_COUNT = 40
MAX_RAY_DISTANCE = 20.0
PLAYER_RADIUS = 0.20
HIT_RADIUS = 0.32

ARENA = (
    "##############",
    "#.....##.....#",
    "#.##..##..##.#",
    "#.#........#.#",
    "#...##..##...#",
    "#............#",
    "##..#....#..##",
    "##..#....#..##",
    "#............#",
    "#...##..##...#",
    "#.#........#.#",
    "#.##..##..##.#",
    "#.....##.....#",
    "##############",
)

MAP_HEIGHT = len(ARENA)
MAP_WIDTH = len(ARENA[0])

SPAWNS = (
    (1.5, 1.5, 0.25 * math.pi),
    (12.5, 1.5, 0.75 * math.pi),
    (12.5, 12.5, -0.75 * math.pi),
    (1.5, 12.5, -0.25 * math.pi),
)


def clamp(value, low, high):
    return max(low, min(high, value))


def wrap_angle(angle):
    return (angle + math.pi) % TAU - math.pi


def angle_difference(left, right):
    return wrap_angle(left - right)


def is_wall_cell(cell_x, cell_y):
    if cell_x < 0 or cell_y < 0 or cell_x >= MAP_WIDTH or cell_y >= MAP_HEIGHT:
        return True
    return ARENA[cell_y][cell_x] == "#"


def position_is_open(x, y, radius=PLAYER_RADIUS):
    """Return whether a circle fits without touching a wall."""
    for check_x in (x - radius, x + radius):
        for check_y in (y - radius, y + radius):
            if is_wall_cell(int(math.floor(check_x)), int(math.floor(check_y))):
                return False
    return True


def move_with_collision(x, y, dx, dy, radius=PLAYER_RADIUS):
    """Move along each axis independently to slide against walls."""
    next_x = x + dx
    if position_is_open(next_x, y, radius):
        x = next_x
    next_y = y + dy
    if position_is_open(x, next_y, radius):
        y = next_y
    return x, y


def cast_ray(x, y, angle, maximum=MAX_RAY_DISTANCE):
    """Cast one grid ray with DDA and return (distance, side)."""
    ray_x = math.cos(angle)
    ray_y = math.sin(angle)
    map_x = int(math.floor(x))
    map_y = int(math.floor(y))

    delta_x = abs(1.0 / ray_x) if abs(ray_x) > 0.000001 else 1000000.0
    delta_y = abs(1.0 / ray_y) if abs(ray_y) > 0.000001 else 1000000.0

    if ray_x < 0.0:
        step_x = -1
        side_x = (x - map_x) * delta_x
    else:
        step_x = 1
        side_x = (map_x + 1.0 - x) * delta_x

    if ray_y < 0.0:
        step_y = -1
        side_y = (y - map_y) * delta_y
    else:
        step_y = 1
        side_y = (map_y + 1.0 - y) * delta_y

    distance = 0.0
    side = 0
    while distance <= maximum:
        if side_x < side_y:
            map_x += step_x
            distance = side_x
            side_x += delta_x
            side = 0
        else:
            map_y += step_y
            distance = side_y
            side_y += delta_y
            side = 1
        if is_wall_cell(map_x, map_y):
            return min(distance, maximum), side
    return maximum, side


class RayCaster:
    """A preallocated forty-column ray caster."""

    def __init__(self, count=RAY_COUNT):
        self.count = count
        self.depths = [MAX_RAY_DISTANCE] * count
        self.shades = [0] * count

    def cast(self, x, y, heading):
        start = heading - FOV * 0.5
        divisor = max(1, self.count - 1)
        for index in range(self.count):
            ray_angle = start + FOV * index / divisor
            raw_distance, side = cast_ray(x, y, ray_angle)
            corrected = raw_distance * math.cos(ray_angle - heading)
            self.depths[index] = max(0.01, corrected)
            shade = int(corrected * 0.8)
            if side:
                shade += 1
            self.shades[index] = int(clamp(shade, 0, 5))
        return self.depths, self.shades


def target_distance_on_ray(origin_x, origin_y, heading, target_x, target_y):
    """Return forward distance when a target circle intersects the aim ray."""
    dx = target_x - origin_x
    dy = target_y - origin_y
    forward = dx * math.cos(heading) + dy * math.sin(heading)
    if forward <= 0.0:
        return None
    lateral = abs(-dx * math.sin(heading) + dy * math.cos(heading))
    if lateral > HIT_RADIUS:
        return None
    return forward


def first_visible_target(origin_x, origin_y, heading, targets, maximum=8.0):
    """Find the nearest (identifier, x, y) target before the first wall."""
    wall_distance, _ = cast_ray(origin_x, origin_y, heading, maximum)
    best_id = None
    best_distance = min(maximum, wall_distance)
    for target_id, target_x, target_y in targets:
        distance = target_distance_on_ray(
            origin_x, origin_y, heading, target_x, target_y
        )
        if distance is not None and distance < best_distance:
            best_id = target_id
            best_distance = distance
    return best_id, best_distance


def wall_runs():
    """Return horizontal wall runs for an inexpensive tactical map."""
    runs = []
    for row, line in enumerate(ARENA):
        start = None
        for column in range(MAP_WIDTH + 1):
            wall = column < MAP_WIDTH and line[column] == "#"
            if wall and start is None:
                start = column
            elif not wall and start is not None:
                runs.append((start, row, column - start))
                start = None
    return tuple(runs)


WALL_RUNS = wall_runs()
