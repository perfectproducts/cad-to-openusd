"""Orbit camera producing USD row-vector camera matrices (`omni:xform`)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Lens authored on the viewer camera (see renderer_runtime.viewer_root_usda).
FOCAL_LENGTH = 18.15
H_APERTURE = 20.955


def look_at(eye, target, up) -> np.ndarray:
    eye, target, up = (np.asarray(v, dtype=np.float64) for v in (eye, target, up))
    f = target - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    if np.linalg.norm(s) < 1e-9:  # looking straight along up: pick any side vector
        s = np.cross(f, np.roll(up, 1))
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3], m[3, :3] = s, u, -f, eye  # camera looks down its -Z
    return m


@dataclass
class OrbitCamera:
    up_axis: str = "Z"
    target: np.ndarray = field(default_factory=lambda: np.zeros(3))
    distance: float = 10.0
    yaw: float = math.radians(-60.0)
    pitch: float = math.radians(25.0)
    aspect: float = 16 / 9
    min_distance: float = 1e-3
    max_distance: float = 1e7

    @property
    def _basis(self):
        if self.up_axis.upper() == "Y":
            return np.array([0.0, 0, 1]), np.array([1.0, 0, 0]), np.array([0.0, 1, 0])
        return np.array([1.0, 0, 0]), np.array([0.0, 1, 0]), np.array([0.0, 0, 1])

    @property
    def vfov(self) -> float:
        v_aperture = H_APERTURE / self.aspect
        return 2.0 * math.atan(0.5 * v_aperture / FOCAL_LENGTH)

    @property
    def hfov(self) -> float:
        return 2.0 * math.atan(0.5 * H_APERTURE / FOCAL_LENGTH)

    def eye(self) -> np.ndarray:
        a, b, u = self._basis
        cp = math.cos(self.pitch)
        d = cp * math.cos(self.yaw) * a + cp * math.sin(self.yaw) * b + math.sin(self.pitch) * u
        return self.target + self.distance * d

    def matrix(self) -> np.ndarray:
        return look_at(self.eye(), self.target, self._basis[2])

    def fit(self, bounds, *, reset_angles: bool = True) -> None:
        lo, hi = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
        self.target = (lo + hi) * 0.5
        radius = max(float(np.linalg.norm(hi - lo)) * 0.5, 1e-6)
        half = 0.5 * min(self.vfov, self.hfov)
        self.distance = radius / math.sin(half) * 1.05
        self.min_distance = radius * 1e-3
        self.max_distance = radius * 50.0
        if reset_angles:
            self.yaw, self.pitch = math.radians(-60.0), math.radians(25.0)

    def orbit(self, dx_px: float, dy_px: float) -> None:
        self.yaw -= dx_px * 0.006
        self.pitch = min(math.radians(89.0), max(math.radians(-89.0), self.pitch + dy_px * 0.006))

    def pan(self, dx_px: float, dy_px: float, viewport_h_px: float) -> None:
        m = self.matrix()
        right, up = m[0, :3], m[1, :3]
        world_per_px = 2.0 * self.distance * math.tan(0.5 * self.vfov) / max(1.0, viewport_h_px)
        self.target = self.target + (-dx_px * right + dy_px * up) * world_per_px

    def dolly(self, factor: float) -> None:
        self.distance = min(self.max_distance, max(self.min_distance, self.distance * factor))
