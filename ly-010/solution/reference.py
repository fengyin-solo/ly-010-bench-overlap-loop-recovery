#!/usr/bin/env python3
"""Recover anonymous loops from overlapping sparse step responses."""

from __future__ import annotations

import json
import itertools
import math
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT_KEYS = {
    "actuators",
    "bench_id",
    "command_bus",
    "loops",
    "sample_count",
    "sample_period_s",
    "schema",
    "session_id",
}
INPUT_ROOT_KEYS = ROOT_KEYS | {"thresholds"}
ACTUATOR_KEYS = {
    "actuator_id",
    "class",
    "delay_range_s",
    "gain_range",
    "tau_range_s",
}
LOOP_KEYS = {"loop_id", "samples"}
THRESHOLD_KEYS = {
    "event_jump_min",
    "event_support_min",
    "fit_rmse_max",
    "mapping_gap_min",
}
ID_PATTERN = re.compile(r"^[A-Z]{2}-[A-Z0-9]{4}-[A-Z0-9]{2}$")
MAX_LOOPS = 4
MIN_LOOPS = 4
FINE_TAU_STEPS = 61
COARSE_TAU_STEPS = 11


class InputFailure(Exception):
    """The public session shape is not satisfied."""


class EventFailure(Exception):
    """The overlapping events do not yield a unique loop grouping."""


class FitFailure(Exception):
    """The recovered model does not meet the published residual limit."""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise InputFailure(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def canonical_bytes(value: object, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def round_half_up(value: float, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def closed_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InputFailure(f"{label} must use its published key set")
    return value


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputFailure(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise InputFailure(f"{label} must be finite")
    return number


def finite_vector(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise InputFailure(f"{label} must be a non-empty array")
    return [
        finite_number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]


def bounded_pair(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise InputFailure(f"{label} must contain two bounds")
    low = finite_number(value[0], f"{label}[0]")
    high = finite_number(value[1], f"{label}[1]")
    if low > high:
        raise InputFailure(f"{label} bounds are reversed")
    return low, high


def identifier(value: Any, label: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise InputFailure(f"{label} must be a text identifier")
    if len(value) < 7 or any(character.isspace() for character in value):
        raise InputFailure(f"{label} must be a compact identifier")
    return value


def load_session(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputFailure("session cannot be read") from exc
    try:
        text = raw.decode("utf-8")
        session = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputFailure("session is not UTF-8 JSON") from exc
    if not isinstance(session, dict):
        raise InputFailure("session root must be an object")
    return session


def validate_session(session: dict[str, Any]) -> None:
    closed_object(session, INPUT_ROOT_KEYS, "root")
    if session["schema"] != "overlap-recovery/v1":
        raise InputFailure("unsupported schema")
    identifier(session["session_id"], "session_id", "BENCH-")
    identifier(session["bench_id"], "bench_id", "HEX-")
    sample_count = session["sample_count"]
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise InputFailure("sample_count must be an integer")
    if sample_count < 64:
        raise InputFailure("sample_count is too small")
    sample_period = finite_number(session["sample_period_s"], "sample_period_s")
    if sample_period <= 0.0:
        raise InputFailure("sample_period_s must be positive")

    command_bus = finite_vector(session["command_bus"], "command_bus")
    if len(command_bus) != sample_count:
        raise InputFailure("command_bus length does not match sample_count")
    if abs(command_bus[0]) > 1e-9:
        raise InputFailure("command_bus must begin at zero")

    actuators = session["actuators"]
    loops = session["loops"]
    if not isinstance(actuators, list) or not MIN_LOOPS <= len(actuators) <= MAX_LOOPS:
        raise InputFailure("actuator count is outside 4..4")
    if not isinstance(loops, list) or len(loops) != len(actuators):
        raise InputFailure("loop and actuator counts differ")

    actuator_ids = []
    for index, raw_actuator in enumerate(actuators):
        actuator = closed_object(
            raw_actuator,
            ACTUATOR_KEYS,
            f"actuators[{index}]",
        )
        actuator_ids.append(
            identifier(
                actuator["actuator_id"],
                f"actuators[{index}].actuator_id",
                "AX-",
            )
        )
        if not isinstance(actuator["class"], str) or not actuator["class"]:
            raise InputFailure("actuator class must be non-empty text")
        for key in ("delay_range_s", "gain_range", "tau_range_s"):
            bounded_pair(actuator[key], f"actuators[{index}].{key}")
    if len(set(actuator_ids)) != len(actuator_ids):
        raise InputFailure("actuator identifiers repeat")

    loop_ids = []
    for index, raw_loop in enumerate(loops):
        loop = closed_object(raw_loop, LOOP_KEYS, f"loops[{index}]")
        loop_ids.append(
            identifier(loop["loop_id"], f"loops[{index}].loop_id", "LC-")
        )
        samples = finite_vector(loop["samples"], f"loops[{index}].samples")
        if len(samples) != sample_count:
            raise InputFailure("loop sample count does not match sample_count")
    if len(set(loop_ids)) != len(loop_ids):
        raise InputFailure("loop identifiers repeat")

    thresholds = closed_object(
        session["thresholds"],
        THRESHOLD_KEYS,
        "thresholds",
    )
    event_jump_min = finite_number(
        thresholds["event_jump_min"],
        "event_jump_min",
    )
    event_support_min = finite_number(
        thresholds["event_support_min"],
        "event_support_min",
    )
    fit_rmse_max = finite_number(thresholds["fit_rmse_max"], "fit_rmse_max")
    mapping_gap_min = finite_number(
        thresholds["mapping_gap_min"],
        "mapping_gap_min",
    )
    if event_jump_min <= 0.0 or fit_rmse_max <= 0.0:
        raise InputFailure("event and fit thresholds must be positive")
    if not 0.0 <= event_support_min <= 1.0 or mapping_gap_min < 0.0:
        raise InputFailure("support thresholds are outside their public meaning")


def detect_events(
    command_bus: list[float],
    jump_min: float,
) -> list[tuple[int, float]]:
    events = []
    for index in range(1, len(command_bus)):
        delta = command_bus[index] - command_bus[index - 1]
        if abs(delta) >= jump_min:
            events.append((index, delta))
    if len(events) < MIN_LOOPS * 2:
        raise EventFailure("too few sparse events")
    return events


def command_trace(
    count: int,
    events: list[tuple[int, float]],
) -> list[float]:
    command = [0.0] * count
    current = 0.0
    event_index = 0
    ordered = sorted(events)
    for index in range(count):
        while event_index < len(ordered) and ordered[event_index][0] == index:
            current += ordered[event_index][1]
            event_index += 1
        command[index] = current
    return command


def tau_candidates(
    actuators: list[dict[str, Any]],
    steps: int,
) -> list[float]:
    lows = [float(item["tau_range_s"][0]) for item in actuators]
    highs = [float(item["tau_range_s"][1]) for item in actuators]
    low = max(0.01, min(lows))
    high = max(highs)
    if high <= low:
        return [low]
    return [
        low + (high - low) * index / (steps - 1)
        for index in range(steps)
    ]


def delay_candidates(
    actuators: list[dict[str, Any]],
    sample_period: float,
) -> list[int]:
    lows = [float(item["delay_range_s"][0]) for item in actuators]
    highs = [float(item["delay_range_s"][1]) for item in actuators]
    low_step = max(0, math.floor(min(lows) / sample_period))
    high_step = max(low_step, math.ceil(max(highs) / sample_period))
    return list(range(low_step, high_step + 1))


def filtered_response(
    command: list[float],
    tau_s: float,
    delay_steps: int,
    sample_period: float,
) -> list[float]:
    alpha = sample_period / (tau_s + sample_period)
    state = 0.0
    response = []
    for index in range(len(command)):
        source = command[index - delay_steps] if index >= delay_steps else 0.0
        state += alpha * (source - state)
        response.append(state)
    return response


def linear_fit(values: list[float], response: list[float]) -> tuple[float, float, float]:
    count = len(values)
    value_mean = math.fsum(values) / count
    response_mean = math.fsum(response) / count
    centered_response = [item - response_mean for item in response]
    variance = math.fsum(item * item for item in centered_response)
    if variance <= 1e-18:
        residual = math.fsum((item - value_mean) ** 2 for item in values)
        return 0.0, value_mean, math.sqrt(residual / count)
    covariance = math.fsum(
        (values[index] - value_mean) * centered_response[index]
        for index in range(count)
    )
    gain = covariance / variance
    baseline = value_mean - gain * response_mean
    residual = math.fsum(
        (
            values[index]
            - baseline
            - gain * response[index]
        )
        ** 2
        for index in range(count)
    )
    return gain, baseline, math.sqrt(residual / count)


def fit_loop(
    values: list[float],
    command: list[float],
    actuators: list[dict[str, Any]],
    sample_period: float,
    steps: int,
) -> dict[str, Any]:
    best = {
        "gain": 0.0,
        "tau_s": 0.0,
        "delay_s": 0.0,
        "rmse": math.inf,
        "sse": math.inf,
    }
    for tau_s in tau_candidates(actuators, steps):
        for delay_steps in delay_candidates(actuators, sample_period):
            response = filtered_response(
                command,
                tau_s,
                delay_steps,
                sample_period,
            )
            gain, _, rmse = linear_fit(values, response)
            sse = rmse * rmse * len(values)
            candidate = (rmse, abs(gain), tau_s, delay_steps)
            current = (
                best["rmse"],
                abs(float(best["gain"])),
                float(best["tau_s"]),
                round(float(best["delay_s"]) / sample_period),
            )
            if candidate < current:
                best = {
                    "gain": gain,
                    "tau_s": tau_s,
                    "delay_s": delay_steps * sample_period,
                    "rmse": rmse,
                    "sse": sse,
                }
    return best


def centered(values: list[float]) -> list[float]:
    mean = math.fsum(values) / len(values)
    return [value - mean for value in values]


def small_solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(matrix)
    augmented = [
        list(matrix[row]) + [vector[row]]
        for row in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [
                    augmented[row][index] - factor * augmented[column][index]
                    for index in range(size + 1)
                ]
    return [augmented[row][size] for row in range(size)]


def subset_fit(
    values: list[float],
    columns: list[list[float]],
) -> tuple[float, list[float]]:
    centered_values = centered(values)
    centered_columns = [centered(column) for column in columns]
    gram = [
        [
            math.fsum(
                first[index] * second[index]
                for index in range(len(values))
            )
            for second in centered_columns
        ]
        for first in centered_columns
    ]
    rhs = [
        math.fsum(
            column[index] * centered_values[index]
            for index in range(len(values))
        )
        for column in centered_columns
    ]
    coefficients = small_solve(gram, rhs)
    if coefficients is None:
        return math.inf, []
    residual = 0.0
    for index in range(len(values)):
        prediction = math.fsum(
            coefficients[column] * centered_columns[column][index]
            for column in range(len(columns))
        )
        residual += (centered_values[index] - prediction) ** 2
    return math.sqrt(residual / len(values)), coefficients


def select_sparse_events(
    loops: list[dict[str, Any]],
    events: list[tuple[int, float]],
    actuators: list[dict[str, Any]],
    sample_period: float,
) -> tuple[list[list[tuple[int, float]]], list[dict[str, Any]], float]:
    count = len(loops[0]["samples"])
    tau_values = tau_candidates(actuators, COARSE_TAU_STEPS)
    delay_values = delay_candidates(actuators, sample_period)
    event_bases = {
        tau_s: {
            delay_steps: [
                centered(
                    filtered_response(
                        command_trace(count, [event]),
                        tau_s,
                        delay_steps,
                        sample_period,
                    )
                )
                for event in events
            ]
            for delay_steps in delay_values
        }
        for tau_s in tau_values
    }
    event_grams = {
        tau_s: {
            delay_steps: [
                [
                    math.fsum(
                        event_bases[tau_s][delay_steps][row][index]
                        * event_bases[tau_s][delay_steps][column][index]
                        for index in range(count)
                    )
                    for column in range(len(events))
                ]
                for row in range(len(events))
            ]
            for delay_steps in delay_values
        }
        for tau_s in tau_values
    }
    selected_by_loop = []
    coarse_models = []
    for loop in loops:
        values = [float(value) for value in loop["samples"]]
        centered_values = centered(values)
        energy = math.fsum(value * value for value in centered_values)
        best: tuple[float, float, int, tuple[int, ...]] | None = None
        for tau_s in tau_values:
            for delay_steps in delay_values:
                bases = event_bases[tau_s][delay_steps]
                support = [
                    math.fsum(
                        base[index] * centered_values[index]
                        for index in range(len(values))
                    )
                    for base in bases
                ]
                gram = event_grams[tau_s][delay_steps]
                for selected in itertools.combinations(range(len(events)), 3):
                    local_gram = [
                        [gram[row][column] for column in selected]
                        for row in selected
                    ]
                    local_support = [support[index] for index in selected]
                    coefficients = small_solve(local_gram, local_support)
                    if coefficients is None:
                        continue
                    explained = math.fsum(
                        coefficients[position] * local_support[position]
                        for position in range(3)
                    )
                    residual_energy = max(0.0, energy - explained)
                    fit_rmse = math.sqrt(residual_energy / len(values))
                    candidate_key = (
                        fit_rmse,
                        tau_s,
                        delay_steps,
                        selected,
                    )
                    if best is None or candidate_key < best:
                        best = candidate_key
        if best is None:
            raise EventFailure("no sparse event subset was found")
        fit_rmse, tau_s, delay_steps, selected = best
        selected_by_loop.append([events[index] for index in selected])
        coarse_models.append(
            {
                "delay_s": delay_steps * sample_period,
                "gain": 0.0,
                "rmse": fit_rmse,
                "sse": fit_rmse * fit_rmse * len(values),
                "tau_s": tau_s,
            }
        )

    flattened = [
        event
        for group in selected_by_loop
        for event in group
    ]
    if len(flattened) != len(events) or len(set(flattened)) != len(events):
        starts = [
            [event[0] for event in group]
            for group in selected_by_loop
        ]
        raise EventFailure(f"independent event groups do not form a partition: {starts}")

    support_margins = []
    for loop_index, group in enumerate(selected_by_loop):
        values = [float(value) for value in loops[loop_index]["samples"]]
        model = coarse_models[loop_index]
        response = filtered_response(
            command_trace(count, group),
            float(model["tau_s"]),
            round(float(model["delay_s"]) / sample_period),
            sample_period,
        )
        _, _, best_rmse = linear_fit(values, response)
        unused = [
            event for event in events if event not in group
        ]
        alternatives = []
        for removed in group:
            kept = [event for event in group if event != removed]
            for candidate in unused:
                alt_response = filtered_response(
                    command_trace(count, kept + [candidate]),
                    float(model["tau_s"]),
                    round(float(model["delay_s"]) / sample_period),
                    sample_period,
                )
                _, _, alt_rmse = linear_fit(values, alt_response)
                alternatives.append(alt_rmse)
        if not alternatives:
            raise EventFailure("no alternative event group remains")
        second_rmse = min(alternatives)
        support_margins.append(
            max(0.0, (second_rmse - best_rmse) / max(second_rmse, 1e-12))
        )
    return selected_by_loop, coarse_models, min(support_margins)


def band_penalty(
    value: float,
    bounds: list[float],
) -> float:
    low = float(bounds[0])
    high = float(bounds[1])
    if low <= value <= high:
        return 0.0
    scale = max(high - low, 0.05)
    distance = low - value if value < low else value - high
    return (distance / scale) ** 2


def mapping_cost(
    fit: dict[str, Any],
    actuator: dict[str, Any],
) -> float:
    penalty = (
        band_penalty(float(fit["gain"]), actuator["gain_range"])
        + band_penalty(float(fit["tau_s"]), actuator["tau_range_s"])
        + band_penalty(float(fit["delay_s"]), actuator["delay_range_s"])
    )
    return float(fit["rmse"]) + 20.0 * penalty


def best_assignments(
    loops: list[dict[str, Any]],
    fits: list[dict[str, Any]],
    actuators: list[dict[str, Any]],
) -> tuple[tuple[int, ...], float, float]:
    ordered_loop_indexes = sorted(
        range(len(loops)),
        key=lambda index: loops[index]["loop_id"],
    )
    ordered_actuators = sorted(
        actuators,
        key=lambda item: item["actuator_id"],
    )
    size = len(loops)
    states: dict[int, list[tuple[float, tuple[int, ...]]]] = {0: [(0.0, ())]}
    for position, loop_index in enumerate(ordered_loop_indexes):
        next_states: dict[int, list[tuple[float, tuple[int, ...]]]] = {}
        for mask, partials in states.items():
            for actuator_index, actuator in enumerate(ordered_actuators):
                bit = 1 << actuator_index
                if mask & bit:
                    continue
                edge = mapping_cost(fits[loop_index], actuator)
                new_mask = mask | bit
                bucket = next_states.setdefault(new_mask, [])
                for partial_cost, mapping in partials:
                    bucket.append(
                        (
                            partial_cost + edge,
                            mapping + (actuator_index,),
                        )
                    )
                bucket.sort(key=lambda item: (item[0], item[1]))
                next_states[new_mask] = bucket[:2]
        states = next_states
    final = states[(1 << size) - 1]
    if len(final) < 2:
        raise EventFailure("mapping has no runner-up")
    best_cost, best_mapping = final[0]
    second_cost = final[1][0]
    return best_mapping, best_cost, second_cost


def recover(session: dict[str, Any]) -> dict[str, Any]:
    sample_count = int(session["sample_count"])
    sample_period = float(session["sample_period_s"])
    loops = session["loops"]
    actuators = session["actuators"]
    thresholds = session["thresholds"]
    events = detect_events(
        [float(value) for value in session["command_bus"]],
        float(thresholds["event_jump_min"]),
    )
    assigned, coarse_fits, support_margin = select_sparse_events(
        loops,
        events,
        actuators,
        sample_period,
    )
    counts = [len(group) for group in assigned]
    if any(count != 3 for count in counts):
        raise EventFailure("event count is not three")
    if support_margin < float(thresholds["event_support_min"]):
        raise EventFailure("event support does not separate the loops")

    fits = []
    for loop_index, loop in enumerate(loops):
        command = command_trace(sample_count, assigned[loop_index])
        fits.append(
            fit_loop(
                [float(value) for value in loop["samples"]],
                command,
                actuators,
                sample_period,
                FINE_TAU_STEPS,
            )
        )
    worst_rmse = max(float(fit["rmse"]) for fit in fits)
    if worst_rmse > float(thresholds["fit_rmse_max"]):
        raise FitFailure("model residual exceeds the public limit")

    ordered_loop_indexes = sorted(
        range(len(loops)),
        key=lambda index: loops[index]["loop_id"],
    )
    ordered_actuators = sorted(
        actuators,
        key=lambda item: item["actuator_id"],
    )
    mapping, best_cost, second_cost = best_assignments(loops, fits, actuators)
    mapping_gap = second_cost - best_cost
    if mapping_gap < float(thresholds["mapping_gap_min"]) and mapping_gap > 1e-12:
        raise EventFailure("mapping alternatives are too close")

    rows = []
    for position, loop_index in enumerate(ordered_loop_indexes):
        loop = loops[loop_index]
        actuator = ordered_actuators[mapping[position]]
        fit = fits[loop_index]
        rows.append(
            {
                "actuator_id": actuator["actuator_id"],
                "delay_s": round_half_up(float(fit["delay_s"]), 6),
                "event_count": counts[loop_index],
                "fit_rmse": round_half_up(float(fit["rmse"]), 6),
                "gain": round_half_up(float(fit["gain"]), 6),
                "loop_id": loop["loop_id"],
                "tau_s": round_half_up(float(fit["tau_s"]), 6),
            }
        )
    return {
        "analysis": {
            "event_count": len(events),
            "event_support_margin": round_half_up(support_margin, 6),
            "global_rmse": round_half_up(worst_rmse, 6),
            "mapping_margin": round_half_up(mapping_gap, 6),
        },
        "bench_id": session["bench_id"],
        "loops": rows,
        "schema_version": 1,
        "session_id": session["session_id"],
    }


def publish_result(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("REFUSED INPUT_SCHEMA\n")
        return 0
    session_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        session = load_session(session_path)
        validate_session(session)
        result = recover(session)
        publish_result(result_path, canonical_bytes(result, newline=True))
    except InputFailure:
        sys.stderr.write("REFUSED INPUT_SCHEMA\n")
        return 0
    except EventFailure:
        sys.stderr.write("REFUSED EVENT_SUPPORT\n")
        return 0
    except FitFailure:
        sys.stderr.write("REFUSED FIT_LIMIT\n")
        return 0
    except OSError:
        return 1
    sys.stdout.write(
        f"RECOVERED {result['session_id']} {len(result['loops'])}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
