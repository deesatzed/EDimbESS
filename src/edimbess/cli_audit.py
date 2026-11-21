"""CLI audit runner generating the cryptographic Statistical Safety Engine verification receipt."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from edimbess.streaming_safety import EDStatisticalSafetyEngine


def build_seed_packet_audit(engine: EDStatisticalSafetyEngine) -> dict[str, Any]:
    """Verify all 22 synthetic ED seed packets against Delta validator, Safe-Gate, and VOI rules."""
    evaluated = engine.evaluate_seed_queue()
    gate_counts: Counter[str] = Counter()
    validator_failures: list[dict[str, Any]] = []
    packet_rows: list[dict[str, Any]] = []

    for pkt in evaluated:
        sg = pkt["safe_gate"]
        vr = pkt["voi_routing"]
        sw = pkt["swarm"]
        gate_counts[sg["gate_state"]] += 1
        if not sw["auditor"]["passed"]:
            validator_failures.append(
                {"packet_id": pkt["packet_id"], "errors": sw["auditor"]["errors"]}
            )
        packet_rows.append(
            {
                "packet_id": pkt["packet_id"],
                "charted_esi": pkt["encounter"]["triage_status"],
                "proposed_esi": pkt["recommendations"][0]["value"]["triage_status"],
                "n_sentinels": len(pkt["sentinels"]),
                "delta_priority": sw["synthesizer"]["priority"],
                "n_orthogonal_findings": sw["orthogonality"]["n_orthogonal_selected"],
                "distinct_mechanisms": sw["orthogonality"]["distinct_mechanisms_covered"],
                "gate_state": sg["gate_state"],
                "allow_one_pinch_routine": sg["allow_one_pinch_routine"],
                "p1_calibrated": sg["p1_calibrated"],
                "p2_calibrated": sg["p2_calibrated"],
                "bundle_voi": vr["bundle_voi"],
                "auto_triggered_channels": vr["auto_triggered_channels"],
                "triage_voi_zero_already_above_tau": vr["triage_voi_zero_already_above_tau"],
            }
        )

    return {
        "n_seed_packets": len(evaluated),
        "all_validator_passed": len(validator_failures) == 0,
        "validator_failures": validator_failures,
        "gate_state_counts": dict(gate_counts),
        "packets": packet_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run full EDimbESS Statistical Safety Engine verification & write audit receipt."
    )
    repo_root = Path(__file__).resolve().parents[2]
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "artifacts" / "safety_engine_audit.json",
        help="Path to output JSON audit receipt.",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=4,
        help="Number of independent replication seeds for Stage-A two-phase benchmark.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print raw JSON receipt to stdout instead of human summary.",
    )
    args = parser.parse_args(argv)

    engine = EDStatisticalSafetyEngine(n_encounters=5600, seed=42)
    cohort_audit = engine.run_full_engine_verification_audit(n_seeds=args.seeds, force=True)
    seed_audit = build_seed_packet_audit(engine)

    full_receipt = {
        **cohort_audit,
        "seed_packet_verification": seed_audit,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(full_receipt, indent=2) + "\n", encoding="utf-8")

    if args.json_only:
        print(json.dumps(full_receipt, indent=2))
        return 0

    cs = full_receipt["cohort_summary"]
    sa = full_receipt["stage_a_two_phase_discovery"]
    st = sa["stress_pool_audit"]
    sb = full_receipt["stage_b_streaming_voi_and_conformal"]
    sg = sb["dual_conformal_safe_gate"]
    cf = sb["conformal_fdr_vs_mondrian"]

    print("=" * 84)
    print("EDimbESS Statistical Safety Engine — Verification Receipt")
    print("=" * 84)
    print(f"Split SHA-256         : {full_receipt['split_hash'][:24]}...")
    print(
        f"Cohort (N={cs['n_encounters']})       : prev={cs['natural_prevalence']:.3f} | "
        f"Stage-1 AUROC={cs['stage1_test_auroc']:.4f} -> Stage-2 AUROC={cs['stage2_test_auroc']:.4f}"
    )
    print(
        f"Stage-2 Calibration   : ECE={cs['stage2_test_ece']:.4f} | "
        f"Cox Slope={cs['stage2_test_cal_slope']:.3f}"
    )
    print("-" * 84)
    print(
        f"Stage-A 15% Neyman    : AUROC={sa['mean_auroc_neyman_15pct']:.4f} "
        f"(vs SRS={sa['mean_auroc_srs_15pct']:.4f}, Full={sa['mean_auroc_full_100pct']:.4f}) | "
        f"TOST NI p={sa['tost_auroc_vs_full']['p_noninferiority']:.4g}"
    )
    print(
        f"Stress Pool (12-ch)   : Null={st['sequential_null_selected']} | "
        f"Twin={st['sequential_twin_selected']} | Echo={st['sequential_echo_selected']} "
        f"(Marginal Twin/Echo={st['marginal_twin_or_echo_selected']})"
    )
    print(
        f"Stage-B VOI Routing   : Net Benefit Gain over Top-Risk = "
        f"{sb['voi_vs_top_risk']['nb_advantage_per_1k_over_top_risk']:+.2f} per 1,000 arrivals"
    )
    print(
        f"Conformal FDR (q=0.35): CfBH FDP={cf['cfbh_tie_smoothed']['realized_fdp']:.3f} "
        f"(Recall={cf['cfbh_tie_smoothed']['realized_recall']:.3f}) vs "
        f"Mondrian {{1}} FDP={cf['mondrian_singleton_pos']['realized_fdp']:.3f}"
    )
    print(
        f"Dual Safe-Gate        : tau_safe={sg['tau_safe_routine']:.4f} | "
        f"Certified Routine={sg['test_certified_routine_rate'] * 100:.1f}% | "
        f"Test FOR={sg['test_realized_for']:.4f} (Cal 95% UCB={sg['cal_for_wilson_95_upper']:.4f})"
    )
    print(
        f"22 Seed ED Packets    : Validator 100% Pass={seed_audit['all_validator_passed']} | "
        f"Gate Counts={seed_audit['gate_state_counts']}"
    )
    print(f"Saved Receipt         : {args.output}")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
