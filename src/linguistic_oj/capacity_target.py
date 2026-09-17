"""Capacity projections and model-only necessary-condition checks, never a classroom pass."""

import math


def _target_work(target):
    if target.get("schema_version") != "classroom-capacity-target-v1":
        raise ValueError("unsupported capacity target")
    if target.get("model") != "Qwen/Qwen3.5-9B":
        raise ValueError("capacity target must bind the configured Qwen3.5-9B model")
    students, samples, budget = (target.get(key) for key in (
        "simultaneous_students", "samples_per_submission", "completion_budget_seconds"))
    if (type(students) is not int or students <= 0 or type(samples) is not int or samples <= 0
            or type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0):
        raise ValueError("capacity target must contain positive counts and a finite budget")
    return students, samples, budget


def project_capacity(report, target):
    students, samples, budget = _target_work(target)
    measured = report.get("measurement") or {}
    count, seconds = measured.get("completed_requests"), measured.get("batch_wall_seconds")
    if (report.get("status") != "completed" or not report.get("real_model_requests_started")
            or type(count) is not int or count <= 0 or measured.get("planned_requests") != count
            or measured.get("termination_unconfirmed")
            or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("projection requires a completed, measured model batch")
    work = students * samples
    observed = count / seconds
    return {"kind": "projection_from_small_model_calibration", "required_model_requests": work,
            "required_model_requests_per_second": work / budget,
            "observed_model_requests_per_second": observed,
            "projected_model_seconds_at_observed_rate": work / observed,
            "throughput_factor_needed_at_observed_rate": (work / budget) / observed,
            "is_hardware_upper_bound": False, "is_required_gpu_count": False,
            "end_to_end_acceptance_verified": False}


def assess_model_budget(report, target):
    students, samples, budget = _target_work(target)
    measured = report.get("measurement") or {}
    matches = (report.get("samples_per_repetition") == samples
               and report.get("repetitions") == students
               and report.get("measurement_budget_seconds") == budget
               and measured.get("planned_requests") == students * samples)
    on_time = set()
    for row in measured.get("rows", []):
        elapsed, repetition, position = (row.get(key) for key in (
            "completion_seconds", "repetition", "sample_position"))
        if (row.get("status") == "completed" and type(elapsed) in (int, float)
                and math.isfinite(elapsed) and 0 <= elapsed <= budget
                and type(repetition) is int and 1 <= repetition <= students
                and type(position) is int and 1 <= position <= samples):
            on_time.add((repetition, position))
    valid_model_run = (report.get("real_model_requests_started") is True
                       and report.get("model_identity", {}).get("model") == target.get("model"))
    if not matches or not valid_model_run:
        verdict = "not_tested"
    elif measured.get("termination_unconfirmed"):
        verdict = "incomplete"
    elif len(on_time) == students * samples:
        verdict = "met"
    elif measured.get("model_budget_verdict") in ("incomplete", "interrupted"):
        verdict = measured["model_budget_verdict"]
    else:
        verdict = "not_met"
    return {"kind": "model_only_necessary_condition", "target_workload_matches": matches,
            "necessary_condition": verdict,
            "samples_completed_within_budget": len(on_time) if matches else None,
            "complete_model_work_copies_within_budget": sum(
                all((repetition, position) in on_time for position in range(1, samples + 1))
                for repetition in range(1, students + 1)) if matches else None,
            "distinct_authenticated_students_tested": False,
            "final_grade_delivery_tested": False, "end_to_end_acceptance_verified": False}
