#!/usr/bin/env python3
"""Two-point calibration for the soil moisture sensor.

1. Hold the sensor in dry air            -> this becomes 0 %
2. Put it in a glass of water (up to the line, keep the electronics dry)
                                         -> this becomes 100 %
The two voltages are saved to calibration.json next to this script.
"""

import argparse
import json
import time
from contextlib import closing

from sensor import (CALIBRATION_FILE, I2C_BUS, SMBus, read_average)


def measure(bus, channel, label):
    input(f"\n{label}\nPress Enter when ready...")
    print("Measuring for 5 seconds...", flush=True)
    readings = []
    for _ in range(5):
        v = read_average(bus, channel, samples=20)
        readings.append(v)
        print(f"  {v:.3f} V", flush=True)
        time.sleep(0.5)
    avg = sum(readings) / len(readings)
    print(f"Average: {avg:.3f} V")
    return avg


def main():
    parser = argparse.ArgumentParser(description="Calibrate the soil sensor")
    parser.add_argument("--channel", type=int, default=0, choices=range(4))
    args = parser.parse_args()

    with closing(SMBus(I2C_BUS)) as bus:
        dry = measure(bus, args.channel,
                      "STEP 1: Wipe the sensor and hold it in DRY AIR.")
        wet = measure(bus, args.channel,
                      "STEP 2: Put the sensor in a glass of WATER "
                      "(only up to the white line).")

    if dry - wet < 0.3:
        print(f"\n!! Dry ({dry:.3f} V) and wet ({wet:.3f} V) are too close."
              "\n   Check the sensor wiring (AOUT -> A0) and try again."
              "\n   Nothing was saved.")
        return

    with open(CALIBRATION_FILE, "w") as f:
        json.dump({"dry_volts": round(dry, 4), "wet_volts": round(wet, 4),
                   "channel": args.channel,
                   "date": time.strftime("%Y-%m-%d %H:%M")}, f, indent=2)
    print(f"\nSaved to {CALIBRATION_FILE}")
    print(f"  0 %   (dry)  = {dry:.3f} V")
    print(f"  100 % (water)= {wet:.3f} V")
    print("Now run: python3 sensor.py")


if __name__ == "__main__":
    main()
