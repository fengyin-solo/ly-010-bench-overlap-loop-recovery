#!/usr/bin/env python3
"""Legacy exporter left on the heat-exchanger gateway."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def compact(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def midpoint(bounds: list[float]) -> float:
    return (float(bounds[0]) + float(bounds[1])) / 2.0


def build_result(session: dict) -> dict:
    loops = sorted(session["loops"], key=lambda item: item["loop_id"])
    actuators = sorted(
        session["actuators"],
        key=lambda item: item["actuator_id"],
    )
    rows = []
    for loop, actuator in zip(loops, actuators, strict=True):
        rows.append(
            {
                "actuator_id": actuator["actuator_id"],
                "delay_s": midpoint(actuator["delay_range_s"]),
                "event_count": 0,
                "fit_rmse": 0.0,
                "gain": midpoint(actuator["gain_range"]),
                "loop_id": loop["loop_id"],
                "tau_s": midpoint(actuator["tau_range_s"]),
            }
        )
    return {
        "analysis": {
            "event_count": 0,
            "event_support_margin": 1.0,
            "global_rmse": 0.0,
            "mapping_margin": 1.0,
        },
        "bench_id": session["bench_id"],
        "loops": rows,
        "schema_version": 1,
        "session_id": session["session_id"],
    }


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("REFUSED INPUT_SCHEMA\n")
        return 0
    session_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
        result = build_result(session)
        result_path.write_bytes(compact(result) + b"\n")
    except OSError:
        return 1
    except Exception:
        sys.stderr.write("REFUSED INPUT_SCHEMA\n")
        return 0
    sys.stdout.write(
        f"RECOVERED {result['session_id']} {len(result['loops'])}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
