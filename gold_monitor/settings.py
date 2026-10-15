"""Small, portable settings file; invalid settings never prevent startup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass
class Settings:
    x: int | None = None
    y: int | None = None
    interval: int = 1
    source: str = "swissquote"
    topmost: bool = True
    compact: bool = False
    opacity: float = 1.0
    docked: bool = False
    taskbar_x: int | None = None
    taskbar_y: int | None = None

    @classmethod
    def load(cls, path: Path) -> Settings:
        result = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return result
            for key in ("x", "y", "taskbar_x", "taskbar_y"):
                value = raw.get(key)
                if type(value) is int and -100000 < value < 100000:
                    setattr(result, key, value)
            if type(raw.get("interval")) is int and raw["interval"] in (1, 2, 5, 30, 60, 120):
                result.interval = raw["interval"]
            if raw.get("source") in ("swissquote", "goldapi"):
                result.source = raw["source"]
            else:
                # Upgrade v1's 30-second Gold API setting to the requested live feed.
                result.interval = 1
            if result.source == "goldapi":
                result.interval = max(30, result.interval)
            for key in ("topmost", "compact", "docked"):
                if type(raw.get(key)) is bool:
                    setattr(result, key, raw[key])
            if type(raw.get("opacity")) in (int, float) and raw["opacity"] in (0.8, 0.9, 1.0):
                result.opacity = raw["opacity"]
        except (OSError, ValueError):
            pass
        return result

    def save(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            temporary.replace(path)
            return True
        except OSError:
            return False


def fit_position(x: int, y: int, width: int, height: int,
                 monitors: list[tuple[int, int, int, int]]) -> tuple[int, int]:
    """Keep the entire widget on the closest monitor, including negative origins."""
    def distance(rect: tuple[int, int, int, int]) -> int:
        left, top, right, bottom = rect
        dx = max(left - x, 0, x - (right - width))
        dy = max(top - y, 0, y - (bottom - height))
        return dx * dx + dy * dy

    left, top, right, bottom = min(monitors, key=distance)
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))
