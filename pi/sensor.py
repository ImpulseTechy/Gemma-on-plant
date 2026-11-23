#!/usr/bin/env python3
"""Read a soil moisture sensor through an ADS1115 / ADS1015 ADC over I2C.

Works with both chips without changing anything: we always read the 16-bit
conversion register and turn it into volts. The ADS1015 (12-bit) left-aligns
its result in the same 16 bits, so the voltage math is identical.

Run directly for a live readout:
    python3 sensor.py              # read forever, once per second
    python3 sensor.py --count 10   # 10 readings then stop
"""

import argparse
import json
import os
import time
from contextlib import closing

try:
    from smbus2 import SMBus
except ImportError:  # Ubuntu apt package "python3-smbus"
    from smbus import SMBus

I2C_BUS = 1          # /dev/i2c-1 = GPIO2 (SDA) / GPIO3 (SCL)
ADC_ADDRESS = 0x48   # ADDR pin tied to GND

REG_CONVERSION = 0x00
REG_CONFIG = 0x01

# Input multiplexer: single-ended channel N vs GND -> MUX bits 100 + N
MUX_SINGLE = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}

# Gain: +/-4.096 V full scale (PGA bits 001). The sensor runs on 3.3 V,
# so its output never exceeds this range.
PGA_BITS = 0b001
FULL_SCALE_VOLTS = 4.096

CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "calibration.json")


def _config_word(channel):
    """Build the 16-bit config register value for one single-shot read."""
    return (
        (1 << 15)                       # OS: start a conversion
        | (MUX_SINGLE[channel] << 12)   # which input to measure
        | (PGA_BITS << 9)               # gain / voltage range
        | (1 << 8)                      # MODE: single-shot
        | (0b100 << 5)                  # data rate: 128 SPS (1115) / 1600 SPS (1015)
        | 0b11                          # comparator off
    )


def read_voltage(bus, channel=0):
    """Take one reading from a channel and return it in volts."""
    config = _config_word(channel)
    bus.write_i2c_block_data(ADC_ADDRESS, REG_CONFIG,
                             [(config >> 8) & 0xFF, config & 0xFF])

    # Wait until the chip says the conversion is finished (OS bit = 1).
    deadline = time.time() + 0.1
    while time.time() < deadline:
        hi, _ = bus.read_i2c_block_data(ADC_ADDRESS, REG_CONFIG, 2)
        if hi & 0x80:
            break
        time.sleep(0.001)

    hi, lo = bus.read_i2c_block_data(ADC_ADDRESS, REG_CONVERSION, 2)
    raw = (hi << 8) | lo
    if raw & 0x8000:            # two's complement -> negative number
        raw -= 1 << 16
    return raw * FULL_SCALE_VOLTS / 32768


def read_average(bus, channel=0, samples=10, gap=0.02):
    """Average several readings to smooth out electrical noise."""
    total = 0.0
    for _ in range(samples):
        total += read_voltage(bus, channel)
        time.sleep(gap)
    return total / samples


def load_calibration():
    """Return {'dry_volts': x, 'wet_volts': y} or None if not calibrated."""
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def volts_to_percent(volts, cal):
    """Capacitive sensors read HIGHER when dry and LOWER when wet."""
    dry, wet = cal["dry_volts"], cal["wet_volts"]
    percent = (dry - volts) / (dry - wet) * 100
    return max(0.0, min(100.0, percent))


def main():
    parser = argparse.ArgumentParser(description="Live soil moisture readout")
    parser.add_argument("--channel", type=int, default=0, choices=range(4),
                        help="ADC input the sensor is on (default A0)")
    parser.add_argument("--count", type=int, default=0,
                        help="number of readings (0 = run until Ctrl-C)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between readings")
    args = parser.parse_args()

    cal = load_calibration()
    if cal is None:
        print("Not calibrated yet: showing volts only (run calibrate.py).")

    n = 0
    # closing(): the apt "smbus" module has no `with` support, smbus2 does.
    with closing(SMBus(I2C_BUS)) as bus:
        try:
            while args.count == 0 or n < args.count:
                v = read_average(bus, args.channel)
                line = f"A{args.channel}: {v:.3f} V"
                if cal:
                    line += f"   moisture: {volts_to_percent(v, cal):5.1f} %"
                print(line, flush=True)
                n += 1
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
