"""
Modbus TCP PLC simulator - water pump station.

Simulates a small water booster station so the collector and dashboard can be
demonstrated without real hardware. Register map matches config.yaml.

Holding registers (function code 3):
    0  suction pressure      x100 bar
    1  discharge pressure    x100 bar
    2  flow rate             x10  m3/h
    3  motor current         x10  A
    4  motor frequency       x10  Hz
    5  tank level            x10  %
    6  total volume (low)    m3
    7  total volume (high)   m3

Discrete inputs (function code 2):
    0  pump running
    1  pump fault
    2  low level switch
    3  high level switch

Usage:
    python simulator/plc_simulator.py --host 0.0.0.0 --port 5020
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

log = logging.getLogger("simulator")

HR_COUNT = 16
DI_COUNT = 16
DEVICE_ID = 0  # single-device server: every incoming device id maps here


class StationModel:
    """Very small process model. Enough to produce believable trends."""

    DEMAND = 30.0              # m3/h drawn from the tank by the network
    TANK_SECONDS_PER_PCT = 360  # tank size, expressed as seconds of net flow per 1 %

    def __init__(self) -> None:
        self.t = 0.0
        self.total_volume = 0.0
        self.running = True
        self.fault = False
        self.tank_level = 62.0

    def step(self, dt: float) -> None:
        self.t += dt

        # Duty cycle: pump stops when the tank is full, restarts when it drains.
        if self.tank_level > 92.0:
            self.running = False
        elif self.tank_level < 45.0:
            self.running = True

        # Rare, self-clearing fault so the alarm path is demonstrable.
        if self.running and random.random() < 0.002:
            self.fault = True
        if self.fault and random.random() < 0.05:
            self.fault = False

        active = self.running and not self.fault

        if active:
            base_flow = 42.0 + 6.0 * math.sin(self.t / 900.0)
            self.flow = max(0.0, base_flow + random.gauss(0, 0.8))
            self.frequency = 47.5 + 2.0 * math.sin(self.t / 1200.0)
            self.current = 18.0 + self.flow * 0.15 + random.gauss(0, 0.2)
            self.discharge = 4.8 + 0.4 * math.sin(self.t / 600.0) + random.gauss(0, 0.05)
            self.suction = 1.6 + random.gauss(0, 0.03)
            self.tank_level += (self.flow - self.DEMAND) * dt / self.TANK_SECONDS_PER_PCT
        else:
            self.flow = 0.0
            self.frequency = 0.0
            self.current = 0.0
            self.discharge = 1.7 + random.gauss(0, 0.02)
            self.suction = 1.6 + random.gauss(0, 0.03)
            self.tank_level -= self.DEMAND * dt / self.TANK_SECONDS_PER_PCT

        self.tank_level = min(100.0, max(0.0, self.tank_level))
        self.total_volume += self.flow * dt / 3600.0

    def holding_registers(self) -> list[int]:
        total = int(self.total_volume)
        regs = [0] * HR_COUNT
        regs[0] = int(self.suction * 100)
        regs[1] = int(self.discharge * 100)
        regs[2] = int(self.flow * 10)
        regs[3] = int(self.current * 10)
        regs[4] = int(self.frequency * 10)
        regs[5] = int(self.tank_level * 10)
        regs[6] = total & 0xFFFF
        regs[7] = (total >> 16) & 0xFFFF
        return regs

    def discrete_inputs(self) -> list[bool]:
        bits = [False] * DI_COUNT
        bits[0] = self.running and not self.fault
        bits[1] = self.fault
        bits[2] = self.tank_level < 20.0
        bits[3] = self.tank_level > 95.0
        return bits


async def update_loop(server: ModbusTcpServer, model: StationModel, dt: float) -> None:
    """Advance the process model and push the new values into the server's datastore."""
    while True:
        model.step(dt)
        # Live writes go through the server, not the context: pymodbus 3.14 moves
        # the datastore into the server once it is constructed.
        await server.async_setValues(DEVICE_ID, 3, 0, model.holding_registers())
        await server.async_setValues(DEVICE_ID, 2, 0, [int(b) for b in model.discrete_inputs()])
        await asyncio.sleep(dt)


async def main(host: str, port: int, interval: float) -> None:
    device = ModbusDeviceContext(
        hr=ModbusSequentialDataBlock(1, [0] * HR_COUNT),
        di=ModbusSequentialDataBlock(1, [0] * DI_COUNT),
        ir=ModbusSequentialDataBlock(1, [0] * HR_COUNT),
        co=ModbusSequentialDataBlock(1, [0] * DI_COUNT),
    )
    context = ModbusServerContext(devices=device, single=True)
    server = ModbusTcpServer(context=context, address=(host, port))
    model = StationModel()

    log.info("PLC simulator listening on %s:%s", host, port)
    task = asyncio.create_task(update_loop(server, model, interval))
    try:
        await server.serve_forever()
    finally:
        task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modbus TCP water pump station simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--interval", type=float, default=1.0, help="model step in seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # pymodbus logs v4 deprecation notices for the classic datastore API on every call
    logging.getLogger("pymodbus").setLevel(logging.ERROR)
    try:
        asyncio.run(main(args.host, args.port, args.interval))
    except KeyboardInterrupt:
        log.info("simulator stopped")
