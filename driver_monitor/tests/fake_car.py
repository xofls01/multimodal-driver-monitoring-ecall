"""팀원 BasicWheelVehicleController 설정을 흉내 낸 간단한 차량 모델 (자전거 모델).

실제 Unity WheelCollider 물리와 같지 않다. 제어 로직이 대략 맞는지 보는 용도.
  maxMotorTorque 1800 x 후륜 2 / 반경 0.35 / 1400kg → 최대 가속 ≈ 7.3 m/s²
  maxBrakeTorque 3500 x 4 → 타이어 한계로 약 9 m/s² 에서 포화로 가정
  maxSteeringAngle 30°, 고속에서 0.45배까지 줄어듦, 조향 입력 변화율 3.5/s, 축거 2.7m
"""
import math


class FakeCar:
    def __init__(self, x=0.75, z=-5.0, kph=0.0):
        self.x, self.z, self.h, self.v = x, z, 0.0, kph / 3.6   # h: rad, +면 오른쪽(+x)으로 향함
        self.steer = 0.0
        self.remote = False

    def apply(self, cmd, dt):
        if cmd.remote_enabled:
            self.remote = True
            req = cmd.steering
            a = cmd.throttle * 7.3 - min(cmd.brake * 28.6, 9.0) * (1 if self.v > 0 else 0) - 0.02 * self.v
        else:
            self.remote = False
            req, a = 0.0, -0.3 if self.v > 0 else 0.0
        step = 3.5 * dt
        self.steer += max(-step, min(step, req - self.steer))
        kph = self.v * 3.6
        delta = math.radians(self.steer * 30.0 * (1.0 - 0.55 * min(1.0, kph / 120.0)))
        self.h += self.v / 2.7 * math.tan(delta) * dt
        self.x += self.v * math.sin(self.h) * dt
        self.z += self.v * math.cos(self.h) * dt
        self.v = max(0.0, self.v + a * dt)

    def state(self):
        return {"type": "vehicle_state", "speed_kph": self.v * 3.6,
                "position": {"x": self.x, "y": 0.43, "z": self.z},
                "forward": {"x": math.sin(self.h), "y": 0.0, "z": math.cos(self.h)},
                "heading_deg": math.degrees(self.h) % 360.0,
                "control_mode": "Remote" if self.remote else "Manual"}
