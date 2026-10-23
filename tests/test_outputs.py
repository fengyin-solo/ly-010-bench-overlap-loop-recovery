from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


TASK_APP = os.environ.get("TASK_APP", "/app/loop_recovery.py")
DATA_DIR = Path(os.environ.get("TASK_DATA_DIR", "/app/data"))
TRUTH = json.loads(
    Path(__file__).with_name("default_truth.json").read_text(encoding="utf-8")
)
RESULT_ROOT_KEYS = {
    "analysis",
    "bench_id",
    "loops",
    "schema_version",
    "session_id",
}
ANALYSIS_KEYS = {
    "event_count",
    "event_support_margin",
    "global_rmse",
    "mapping_margin",
}
LOOP_KEYS = {
    "actuator_id",
    "delay_s",
    "event_count",
    "fit_rmse",
    "gain",
    "loop_id",
    "tau_s",
}


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def read_session() -> dict:
    return json.loads((DATA_DIR / "session.json").read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def invoke(
    session_path: Path,
    result_path: Path,
    timeout: int = 8,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, TASK_APP, str(session_path), str(result_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed, time.monotonic() - started


def run_value(
    directory: Path,
    session: dict,
    tag: str,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    work = directory / tag
    work.mkdir(parents=True, exist_ok=True)
    session_path = work / "session.json"
    result_path = work / "recovery.json"
    write_json(session_path, session)
    completed, _ = invoke(session_path, result_path)
    return completed, session_path, result_path


def read_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def mapping_of(result: dict) -> dict[str, str]:
    return {
        row["loop_id"]: row["actuator_id"]
        for row in result["loops"]
    }


def event_signal(count: int, rows: list[dict]) -> list[float]:
    signal = [0.0] * count
    current = 0.0
    by_start = {
        int(row["start_sample"]): float(row["delta_pct"])
        for row in rows
    }
    for index in range(count):
        current += by_start.get(index, 0.0)
        signal[index] = current
    return signal


def reduced_session(loop_count: int) -> dict:
    session = read_session()
    chosen_loops = session["loops"][:loop_count]
    mapping = {
        loop_id: TRUTH["mapping"][loop_id]
        for loop_id in [loop["loop_id"] for loop in chosen_loops]
    }
    chosen_ids = set(mapping.values())
    session["loops"] = chosen_loops
    session["actuators"] = [
        item
        for item in session["actuators"]
        if item["actuator_id"] in chosen_ids
    ]
    session["command_bus"] = [
        round(
            sum(
                event_signal(
                    session["sample_count"],
                    TRUTH["events"][actuator_id],
                )[index]
                for actuator_id in chosen_ids
            ),
            6,
        )
        for index in range(session["sample_count"])
    ]
    return session


@pytest.fixture(scope="session")
def default_run(tmp_path_factory):
    directory = tmp_path_factory.mktemp("default")
    session_path = DATA_DIR / "session.json"
    result_path = directory / "recovery.json"
    completed, elapsed = invoke(session_path, result_path)
    return completed, elapsed, result_path, session_path


def test_default_recovery_passes_core_gate(default_run):
    """case_id: GATE-01
    题面：「回路到执行器是一一对应。」
    """
    completed, _, result_path, _ = default_run
    result = read_result(result_path)
    assert completed.returncode == 0
    assert mapping_of(result) == TRUTH["mapping"]
    assert result["analysis"]["event_count"] == 12
    assert all(row["event_count"] == 3 for row in result["loops"])


def test_default_mapping_matches_true_actuators(default_run):
    """case_id: MAP-01
    题面：「值班组需要在交接前恢复事件归属、接线和动态参数。」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert mapping_of(result) == TRUTH["mapping"]
    assert len({row["actuator_id"] for row in result["loops"]}) == 4


def test_each_loop_recovers_three_events(default_run):
    """case_id: EVENT-01
    题面：「每路必须恰好恢复 3 个事件，全批共 12 个」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert [row["event_count"] for row in result["loops"]] == [3] * 4
    assert result["analysis"]["event_count"] == 12


def test_event_count_totals_sparse_starts(default_run, tmp_path):
    """case_id: EVENT-02
    题面：「全批共 12 个；四组事件不能重复使用。」
    """
    print("HARDCODE-BASE INPUT_SCENARIO_DEFAULT")
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert sum(row["event_count"] for row in result["loops"]) == 12
    assert result["analysis"]["event_count"] == 12
    assert result["analysis"]["event_support_margin"] >= 0.0

    session = reduced_session(4)
    session["thresholds"]["event_jump_min"] = min(
        abs(after - before)
        for before, after in zip(
            session["command_bus"],
            session["command_bus"][1:],
        )
        if abs(after - before) > 1e-12
    )
    completed, _, boundary_result = run_value(
        tmp_path,
        session,
        "event-threshold-boundary",
    )
    boundary = read_result(boundary_result)
    assert completed.returncode == 0
    assert boundary["analysis"]["event_count"] == 12


def test_command_bus_is_the_superposition():
    """case_id: EVENT-03
    题面：「总指令等于四条自身指令在同一采样周期上的和」
    """
    session = read_session()
    reconstructed = [
        round(
            sum(
                event_signal(
                    session["sample_count"],
                    rows,
                )[index]
                for rows in TRUTH["events"].values()
            ),
            6,
        )
        for index in range(session["sample_count"])
    ]
    assert reconstructed == session["command_bus"]


def test_transition_tails_overlap_for_each_actuator():
    """case_id: EVENT-04
    题面：「每路过渡过程又与相邻事件叠在一起。」
    """
    gaps = [
        rows[index + 1]["start_sample"] - rows[index]["start_sample"]
        for rows in TRUTH["events"].values()
        for index in range(2)
    ]
    parameters = list(TRUTH["parameters"].values())
    overlap_horizon = max(3.0 * item["tau_s"] / 0.1 for item in parameters)
    assert max(gaps) < overlap_horizon


def test_no_per_actuator_command_trace_is_exposed():
    """case_id: EVENT-05
    题面：「单台指令轨迹不随会话提供。」
    """
    session = read_session()
    assert "command_bus" in session
    assert "events" not in session
    assert all(set(item) == {"actuator_id", "class", "delay_range_s", "gain_range", "tau_range_s"} for item in session["actuators"])


def test_gain_values_stay_within_two_percent(default_run):
    """case_id: MODEL-01
    题面：「默认批次还要求增益相对误差不超过 2%」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    relative_errors = [
        abs(
            row["gain"]
            - TRUTH["parameters"][row["actuator_id"]]["gain"]
        )
        / abs(TRUTH["parameters"][row["actuator_id"]]["gain"])
        for row in result["loops"]
    ]
    assert max(relative_errors) <= 0.02


def test_tau_values_stay_within_six_percent(default_run):
    """case_id: MODEL-02
    题面：「时间常数相对误差不超过 6%」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    relative_errors = [
        abs(
            row["tau_s"]
            - TRUTH["parameters"][row["actuator_id"]]["tau_s"]
        )
        / TRUTH["parameters"][row["actuator_id"]]["tau_s"]
        for row in result["loops"]
    ]
    assert max(relative_errors) <= 0.06


def test_delay_values_stay_within_four_hundredths(default_run):
    """case_id: MODEL-03
    题面：「纯时延绝对误差不超过 0.04 秒」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    absolute_errors = [
        abs(
            row["delay_s"]
            - TRUTH["parameters"][row["actuator_id"]]["delay_s"]
        )
        for row in result["loops"]
    ]
    assert max(absolute_errors) <= 0.04 + 1e-9


def test_each_loop_fit_meets_rmse_limit(default_run):
    """case_id: MODEL-04
    题面：「`fit_rmse_max` 是每路均方根误差的上限。」
    """
    _, _, result_path, session_path = default_run
    result = read_result(result_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert max(row["fit_rmse"] for row in result["loops"]) <= session["thresholds"]["fit_rmse_max"]


def test_recovered_parameters_stay_within_public_ranges(default_run):
    """case_id: MODEL-06
    题面：「恢复的动态参数须落在对应执行器的公开范围内。」
    """
    _, _, result_path, session_path = default_run
    result = read_result(result_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    ranges = {
        item["actuator_id"]: item
        for item in session["actuators"]
    }
    assert all(
        ranges[row["actuator_id"]]["gain_range"][0]
        <= row["gain"]
        <= ranges[row["actuator_id"]]["gain_range"][1]
        for row in result["loops"]
    )
    assert all(
        ranges[row["actuator_id"]]["tau_range_s"][0]
        <= row["tau_s"]
        <= ranges[row["actuator_id"]]["tau_range_s"][1]
        for row in result["loops"]
    )
    assert all(
        ranges[row["actuator_id"]]["delay_range_s"][0]
        <= row["delay_s"]
        <= ranges[row["actuator_id"]]["delay_range_s"][1]
        for row in result["loops"]
    )


def test_global_fit_rmse_is_reported(default_run):
    """case_id: MODEL-05
    题面：「`analysis.global_rmse` 取各路 `fit_rmse` 的最大值」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert result["analysis"]["global_rmse"] == max(
        row["fit_rmse"] for row in result["loops"]
    )


def test_event_support_margin_meets_public_threshold(default_run):
    """case_id: SUPPORT-01
    题面：「最小相对支持差低于 `event_support_min` 时同样关闭为 `EVENT_SUPPORT`。」
    """
    _, _, result_path, session_path = default_run
    result = read_result(result_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert result["analysis"]["event_support_margin"] >= session["thresholds"]["event_support_min"]
    assert result["analysis"]["event_support_margin"] > 0.5


def test_mapping_margin_supports_unique_assignment(default_run):
    """case_id: SUPPORT-02
    题面：「最佳与次佳配对的未舍入总支持差低于 `mapping_gap_min` 时关闭为 `EVENT_SUPPORT`。」
    """
    _, _, result_path, session_path = default_run
    result = read_result(result_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert result["analysis"]["mapping_margin"] >= session["thresholds"]["mapping_gap_min"]
    assert result["analysis"]["mapping_margin"] > 1.0


def test_exact_mapping_tie_uses_lexicographic_order(tmp_path):
    """case_id: CANON-01
    题面：「按结果数组顺序对 `actuator_id` 取规范字典序的首项。」
    """
    session = read_session()
    gain_bounds = [
        min(item["gain_range"][0] for item in session["actuators"]),
        max(item["gain_range"][1] for item in session["actuators"]),
    ]
    tau_bounds = [
        min(item["tau_range_s"][0] for item in session["actuators"]),
        max(item["tau_range_s"][1] for item in session["actuators"]),
    ]
    delay_bounds = [
        min(item["delay_range_s"][0] for item in session["actuators"]),
        max(item["delay_range_s"][1] for item in session["actuators"]),
    ]
    for actuator in session["actuators"]:
        actuator["gain_range"] = gain_bounds
        actuator["tau_range_s"] = tau_bounds
        actuator["delay_range_s"] = delay_bounds
    session["thresholds"]["mapping_gap_min"] = 0.0
    completed, _, result_path = run_value(tmp_path, session, "tie")
    result = read_result(result_path)
    expected = dict(
        zip(
            sorted(item["loop_id"] for item in session["loops"]),
            sorted(item["actuator_id"] for item in session["actuators"]),
            strict=True,
        )
    )
    assert completed.stderr == ""
    assert mapping_of(result) == expected
    assert result["analysis"]["mapping_margin"] == pytest.approx(0.0)


def test_reordered_arrays_keep_the_same_mapping(tmp_path):
    """case_id: ORDER-01
    题面：「`actuators` 与 `loops` 都恰好 4 项，标识不重复。」
    """
    print("HARDCODE-VARIANT INPUT_SCENARIO_REORDERED")
    session = read_session()
    session["loops"].reverse()
    session["actuators"].reverse()
    completed, _, result_path = run_value(tmp_path, session, "reordered")
    result = read_result(result_path)
    assert completed.returncode == 0
    assert mapping_of(result) == TRUTH["mapping"]
    assert result["analysis"]["event_support_margin"] >= 0.0


def test_five_loops_with_short_trace_are_refused(tmp_path):
    """case_id: COUNT-01
    题面：「`actuators` 与 `loops` 都恰好 4 项，标识不重复。」
    """
    session = read_session()
    session["sample_count"] = 128
    session["command_bus"] = session["command_bus"][:128]
    for loop in session["loops"]:
        loop["samples"] = loop["samples"][:128]
    session["loops"].append(copy.deepcopy(session["loops"][0]))
    session["actuators"].append(copy.deepcopy(session["actuators"][0]))
    session["loops"][-1]["loop_id"] = "LC-X999-01"
    session["actuators"][-1]["actuator_id"] = "AX-X999-01"
    completed, _, result_path = run_value(tmp_path, session, "five-short")
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_invalid_root_key_set_is_refused(tmp_path):
    """case_id: INPUT-01
    题面：「各层出现额外键、缺键或重复 JSON 键时关闭为 `INPUT_SCHEMA`。」
    """
    session_path = tmp_path / "session.json"
    result_path = tmp_path / "recovery.json"

    missing = read_session()
    del missing["thresholds"]
    session_path.write_bytes(canonical(missing) + b"\n")
    completed, _ = invoke(session_path, result_path)
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()

    extra = read_session()
    extra["unexpected"] = 1
    session_path.write_bytes(canonical(extra) + b"\n")
    completed, _ = invoke(session_path, result_path)
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()

    original = read_session()
    duplicate = (
        '{"schema":"overlap-recovery/v1",'
        + canonical(original).decode("utf-8")[1:]
    ).encode("utf-8")
    session_path.write_bytes(duplicate)
    completed, _ = invoke(session_path, result_path)
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_sample_length_mismatch_is_refused(tmp_path):
    """case_id: INPUT-02
    题面：「`sample_period_s` 为正数；`command_bus` 与每路 `samples` 的长度都等于 `sample_count`」
    """
    session = read_session()
    session["loops"][0]["samples"].pop()
    completed, _, result_path = run_value(tmp_path, session, "length")
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()

    session = read_session()
    session["sample_period_s"] = 0.0
    completed, _, result_path = run_value(tmp_path, session, "period-zero")
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_boolean_sample_is_refused(tmp_path):
    """case_id: INPUT-03
    题面：「且内容为有限数值。」
    """
    session = read_session()
    session["loops"][0]["samples"][0] = True
    completed, _, result_path = run_value(tmp_path, session, "boolean")
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_non_utf8_session_is_refused(tmp_path):
    """case_id: INPUT-04
    题面：「会话为 UTF-8 JSON」
    """
    session_path = tmp_path / "session.json"
    result_path = tmp_path / "result.json"
    session_path.write_bytes(b"\xff\xfe\x00\x00")
    completed, _ = invoke(session_path, result_path)
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_impossible_fit_limit_is_refused(tmp_path):
    """case_id: FIT-01
    题面：「动态模型超出公开误差上限写 `REFUSED FIT_LIMIT`。」
    """
    session = read_session()
    session["thresholds"]["fit_rmse_max"] = 0.001
    completed, _, result_path = run_value(tmp_path, session, "fit")
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED FIT_LIMIT\n"
    assert not result_path.exists()


def test_weak_event_support_is_refused(tmp_path):
    """case_id: SUPPORT-03
    题面：「配对或事件归属不唯一写 `REFUSED EVENT_SUPPORT`」
    """
    session = read_session()
    session["thresholds"]["event_support_min"] = 0.99
    completed, _, result_path = run_value(tmp_path, session, "support")
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED EVENT_SUPPORT\n"
    assert not result_path.exists()


def test_success_channel_is_exact(default_run):
    """case_id: OUTPUT-01
    题面：「成功时标准输出只有 `RECOVERED <session_id> 4` 一行」
    """
    completed, _, _, _ = default_run
    assert completed.stdout == "RECOVERED BENCH-2026-10-23-HX04 4\n"


def test_refusal_stdout_is_empty(tmp_path):
    """case_id: OUTPUT-03
    题面：「输入不合约时标准输出为空」
    """
    session = read_session()
    del session["schema"]
    completed, _, result_path = run_value(tmp_path, session, "silent")
    assert completed.stdout == ""
    assert not result_path.exists()


def test_refusal_channel_is_exact(tmp_path):
    """case_id: OUTPUT-02
    题面：「标准错误只有 `REFUSED INPUT_SCHEMA` 一行。」
    """
    session = read_session()
    del session["schema"]
    completed, _, result_path = run_value(tmp_path, session, "refusal")
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_unsupported_schema_is_refused(tmp_path):
    """case_id: SCHEMA-04
    题面：「`schema` 是 `overlap-recovery/v1`。」
    """
    session = read_session()
    session["schema"] = "overlap-recovery/v2"
    completed, _, result_path = run_value(tmp_path, session, "schema")
    assert completed.stderr == "REFUSED INPUT_SCHEMA\n"
    assert not result_path.exists()


def test_result_root_keys_are_exact(default_run):
    """case_id: SCHEMA-01
    题面：「result: {analysis, bench_id, loops, schema_version, session_id}」。
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert set(result) == RESULT_ROOT_KEYS
    assert result["schema_version"] == 1


def test_analysis_keys_are_exact(default_run):
    """case_id: SCHEMA-02
    题面：「analysis: {event_count, event_support_margin, global_rmse, mapping_margin}」。
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert set(result["analysis"]) == ANALYSIS_KEYS
    assert isinstance(result["analysis"]["event_count"], int)


def test_loop_row_keys_are_exact(default_run):
    """case_id: SCHEMA-03
    题面：「结果对象的键集合如下：」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    assert all(set(row) == LOOP_KEYS for row in result["loops"])


def test_loop_rows_are_sorted_by_identifier(default_run):
    """case_id: SORT-01
    题面：「`loops` 按 `loop_id` 升序」
    """
    _, _, result_path, _ = default_run
    result = read_result(result_path)
    ids = [row["loop_id"] for row in result["loops"]]
    assert ids == sorted(ids)


def test_result_bytes_are_canonical(default_run):
    """case_id: BYTES-01
    题面：「结果文件采用 UTF-8、键名升序、紧凑 JSON，末尾一个换行。」
    """
    _, _, result_path, _ = default_run
    raw = result_path.read_bytes()
    assert raw == canonical(read_result(result_path)) + b"\n"
    assert not raw.endswith(b"\n\n")


def test_full_batch_completes_within_public_limit(tmp_path):
    """case_id: TIME-01
    题面：「四路、320 点的完整批次须在 6 秒内完成」
    """
    session_path = tmp_path / "session.json"
    result_path = tmp_path / "result.json"
    write_json(session_path, read_session())
    completed, elapsed = invoke(session_path, result_path, timeout=8)
    assert completed.returncode == 0
    assert elapsed < 6.0
    assert result_path.exists()
