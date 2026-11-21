"""HTTP/REST server for EDimbESS PITM XR Web App and Statistical Safety Engine APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from edimbess.sentinels_and_rules import (
    DECISION_TYPES,
    SEED_PACKETS,
    validate_intake_payload,
)
from edimbess.streaming_safety import EDStatisticalSafetyEngine, get_default_safety_engine
from edimbess.two_phase import audit_shadow_ledger_two_phase

DEFAULT_CAPACITY: dict[str, int] = {
    "bedsOpen": 3,
    "bedsTotal": 18,
    "hallwayOpen": 2,
    "hallwayTotal": 8,
    "fastTrackOpen": 2,
    "fastTrackTotal": 4,
    "waitboard": 14,
}


class EDimbESSAppState:
    """Thread-safe application state managing packets, capacity, hash-chained ledger, and engine."""

    def __init__(
        self,
        *,
        engine: EDStatisticalSafetyEngine | None = None,
        web_dir: Path | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        self.engine = engine if engine is not None else get_default_safety_engine()
        self.web_dir = (web_dir if web_dir is not None else repo_root / "web").resolve()
        self.ledger_path = (
            ledger_path if ledger_path is not None else repo_root / "data" / "decisions.jsonl"
        ).resolve()
        self.lock = threading.Lock()
        self.capacity: dict[str, int] = dict(DEFAULT_CAPACITY)
        self.custom_packets: dict[str, dict[str, Any]] = {}
        self.unlocked_channels_by_packet: dict[str, set[str]] = {}

    def all_raw_packets(self) -> list[dict[str, Any]]:
        with self.lock:
            by_id = {pkt["packet_id"]: pkt for pkt in SEED_PACKETS}
            by_id.update(self.custom_packets)
            return list(by_id.values())

    def get_packet_by_id(self, packet_id: str) -> dict[str, Any] | None:
        for pkt in self.all_raw_packets():
            if pkt["packet_id"].lower() == packet_id.lower():
                return pkt
        return None

    def read_ledger(self) -> tuple[list[dict[str, Any]], bool]:
        """Read JSONL ledger and verify unbroken SHA-256 hash chain."""
        with self.lock:
            if not self.ledger_path.exists():
                return [], True
            entries: list[dict[str, Any]] = []
            prev_hash = "GENESIS"
            valid_chain = True
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                rec = json.loads(raw)
                stored_hash = rec.get("entry_hash", "")
                check_payload = {k: v for k, v in rec.items() if k != "entry_hash"}
                expected = hashlib.sha256(
                    json.dumps(check_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if rec.get("prev_hash") != prev_hash or stored_hash != expected:
                    valid_chain = False
                prev_hash = stored_hash
                entries.append(rec)
            return entries, valid_chain

    def append_ledger_entries(self, raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate and append one or more PITM decisions to the SHA-256 hash-chained ledger."""
        if not raw_items:
            raise ValueError("Ledger submission must contain at least one decision entry.")

        validated_batch: list[dict[str, Any]] = []
        for item in raw_items:
            pkt_id = str(item.get("packetId") or item.get("packet_id") or "").strip()
            dec_type = str(item.get("decisionType") or item.get("decision_type") or "").strip()
            action = str(item.get("providerAction") or item.get("provider_action") or "").strip()
            reason = str(item.get("overrideReason") or item.get("override_reason") or "").strip()

            if not pkt_id.lower().startswith("syn-ed-"):
                raise ValueError(f"Invalid packetId '{pkt_id}'. Must match syn-ED-###.")
            if dec_type not in DECISION_TYPES:
                raise ValueError(f"Unsupported decisionType '{dec_type}'.")
            if action not in {"accept", "change", "decline"}:
                raise ValueError(f"Unsupported providerAction '{action}'.")
            if action == "change" and len(reason) < 8:
                raise ValueError(
                    "Changed PITM decisions require an overrideReason of at least 8 characters."
                )

            validated_batch.append(
                {
                    "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "packetId": pkt_id,
                    "decisionType": dec_type,
                    "providerAction": action,
                    "overrideReason": reason if reason else None,
                    "gateState": item.get("gateState"),
                    "modelRecommendation": item.get("modelRecommendation"),
                    "finalValue": item.get("finalValue"),
                }
            )

        with self.lock:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            prev_hash = "GENESIS"
            if self.ledger_path.exists():
                lines = [
                    ln.strip()
                    for ln in self.ledger_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                if lines:
                    prev_hash = json.loads(lines[-1]).get("entry_hash", "GENESIS")

            written: list[dict[str, Any]] = []
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                for base_rec in validated_batch:
                    rec_with_prev = {**base_rec, "prev_hash": prev_hash}
                    entry_hash = hashlib.sha256(
                        json.dumps(rec_with_prev, sort_keys=True, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    final_rec = {**rec_with_prev, "entry_hash": entry_hash}
                    fh.write(json.dumps(final_rec, separators=(",", ":")) + "\n")
                    prev_hash = entry_hash
                    written.append(final_rec)
            return written


def make_handler(state: EDimbESSAppState) -> type[BaseHTTPRequestHandler]:
    """Create a request handler bound to the provided EDimbESSAppState."""

    class EDimbESSHandler(BaseHTTPRequestHandler):
        server_version = "EDimbESS/0.1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep test and CLI output quiet unless explicitly debugging
            return

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValueError("Request payload exceeds 2 MB limit.")
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _serve_static(self, req_path: str) -> None:
            rel = "index.html" if req_path in ("", "/") else req_path.lstrip("/")
            target = (state.web_dir / rel).resolve()
            if not str(target).startswith(str(state.web_dir)) or not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Path '{req_path}' not found."})
                return

            ctype, _ = mimetypes.guess_type(str(target))
            if target.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif target.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif target.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            elif not ctype:
                ctype = "application/octet-stream"

            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/api/health":
                eng = state.engine
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "engine_version": "edimbess-0.1.0",
                        "research_only": True,
                        "split_hash": eng.split.split_hash,
                        "natural_prevalence": round(eng.natural_prevalence, 4),
                        "stage1_test_auroc": round(eng.test_diag_s1.auroc, 4),
                        "stage2_test_auroc": round(eng.test_diag_s2.auroc, 4),
                        "stage2_test_ece": round(eng.test_diag_s2.ece, 4),
                        "stage2_test_cal_slope": round(eng.test_diag_s2.cal_slope, 4),
                        "tau_safe_routine": round(eng.tau_safe, 4),
                        "cal_for_wilson_upper": round(eng.cal_for_wilson_upper, 4),
                        "packet_count": len(state.all_raw_packets()),
                    },
                )
                return

            if route == "/api/packets":
                with state.lock:
                    cap = dict(state.capacity)
                    unlocked_map = {
                        k: sorted(v) for k, v in state.unlocked_channels_by_packet.items()
                    }
                enriched = [
                    state.engine.evaluate_packet_safety(
                        pkt,
                        cap,
                        unlocked_channels=unlocked_map.get(pkt["packet_id"], []),
                    )
                    for pkt in state.all_raw_packets()
                ]
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "capacity": cap,
                        "packets": enriched,
                    },
                )
                return

            if route == "/api/ledger":
                entries, valid_chain = state.read_ledger()
                shadow = audit_shadow_ledger_two_phase(entries)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "count": len(entries),
                        "valid_hash_chain": valid_chain,
                        "entries": entries,
                        "two_phase_shadow_audit": shadow,
                    },
                )
                return

            if route == "/api/safety/audit":
                entries, valid_chain = state.read_ledger()
                audit = state.engine.run_full_engine_verification_audit(n_seeds=3, force=False)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        **audit,
                        "live_ledger_chain_valid": valid_chain,
                        "live_ledger_two_phase_audit": audit_shadow_ledger_two_phase(entries),
                    },
                )
                return

            self._serve_static(route)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path

            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {exc}"})
                return

            if route == "/api/evaluate":
                if isinstance(body.get("capacity"), dict):
                    with state.lock:
                        for k in DEFAULT_CAPACITY:
                            if k in body["capacity"]:
                                state.capacity[k] = max(0, int(body["capacity"][k]))
                        cap = dict(state.capacity)
                else:
                    with state.lock:
                        cap = dict(state.capacity)

                unlocked_req = body.get("unlocked_channels")
                allow_llm = bool(body.get("allow_live_llm", False))

                if "packet" in body:
                    ok, res = validate_intake_payload(body["packet"])
                    if not ok or isinstance(res, str):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": res})
                        return
                    pkt = res[0]
                elif "packet_id" in body:
                    pkt = state.get_packet_by_id(str(body["packet_id"]))
                    if pkt is None:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": f"Packet '{body['packet_id']}' not found."},
                        )
                        return
                else:
                    # Re-evaluate entire queue under updated capacity
                    with state.lock:
                        unlocked_map = {
                            k: sorted(v) for k, v in state.unlocked_channels_by_packet.items()
                        }
                    enriched = [
                        state.engine.evaluate_packet_safety(
                            p,
                            cap,
                            unlocked_channels=unlocked_map.get(p["packet_id"], []),
                            allow_live_llm=allow_llm,
                        )
                        for p in state.all_raw_packets()
                    ]
                    self._send_json(HTTPStatus.OK, {"capacity": cap, "packets": enriched})
                    return

                pid = pkt["packet_id"]
                if isinstance(unlocked_req, list):
                    with state.lock:
                        cur = state.unlocked_channels_by_packet.setdefault(pid, set())
                        cur.update(str(c) for c in unlocked_req)
                        unlocked_list = sorted(cur)
                else:
                    with state.lock:
                        unlocked_list = sorted(state.unlocked_channels_by_packet.get(pid, set()))

                evaluated = state.engine.evaluate_packet_safety(
                    pkt,
                    cap,
                    unlocked_channels=unlocked_list,
                    allow_live_llm=allow_llm,
                )
                self._send_json(HTTPStatus.OK, {"capacity": cap, "packet": evaluated})
                return

            if route == "/api/intake":
                ok, res = validate_intake_payload(body)
                if not ok or isinstance(res, str):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": res})
                    return

                with state.lock:
                    cap = dict(state.capacity)
                    for pkt in res:
                        state.custom_packets[pkt["packet_id"]] = pkt

                evaluated_new = [state.engine.evaluate_packet_safety(pkt, cap) for pkt in res]
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "accepted": len(evaluated_new),
                        "packets": evaluated_new,
                    },
                )
                return

            if route == "/api/ledger":
                items = (
                    body.get("entries")
                    if isinstance(body, dict) and "entries" in body
                    else (body if isinstance(body, list) else [body])
                )
                try:
                    written = state.append_ledger_entries(items)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return

                all_entries, valid_chain = state.read_ledger()
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "written": written,
                        "total_entries": len(all_entries),
                        "valid_hash_chain": valid_chain,
                        "two_phase_shadow_audit": audit_shadow_ledger_two_phase(all_entries),
                    },
                )
                return

            if route == "/api/safety/audit":
                force = bool(body.get("force", False)) if isinstance(body, dict) else False
                n_seeds = int(body.get("n_seeds", 3)) if isinstance(body, dict) else 3
                entries, valid_chain = state.read_ledger()
                audit = state.engine.run_full_engine_verification_audit(
                    n_seeds=max(2, min(6, n_seeds)), force=force
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        **audit,
                        "live_ledger_chain_valid": valid_chain,
                        "live_ledger_two_phase_audit": audit_shadow_ledger_two_phase(entries),
                    },
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint '{route}'."})

    return EDimbESSHandler


def create_http_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    state: EDimbESSAppState | None = None,
) -> ThreadingHTTPServer:
    app_state = state if state is not None else EDimbESSAppState()
    handler_cls = make_handler(app_state)
    return ThreadingHTTPServer((host, port), handler_cls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start EDimbESS PITM XR & Safety Engine server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    args = parser.parse_args(argv)

    print("Calibrating EDimbESS Statistical Safety Engine on leak-free 4-way ED cohort...")
    state = EDimbESSAppState()
    srv = create_http_server(args.host, args.port, state=state)
    actual_port = srv.server_address[1]
    print(f"EDimbESS ready at http://{args.host}:{actual_port}  (Research/Demo — Zero PHI)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down EDimbESS server.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
