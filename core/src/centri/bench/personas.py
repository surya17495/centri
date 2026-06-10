"""centri-bench persona histories — scripted ground truth.

Three project personas (trading system, web app, infra migration), each a
multi-week synthetic history seeded directly into the event ledger as the typed
events of ``docs/event-contract.md``. Each carries the synthesis hints the
consolidation worker reads (``decision`` / ``fact`` / ``open_loop`` /
``loop_resolution``) so the same production capture path builds memory.

Alongside each history we author the ground truth the metrics score against:

  - ``cue``         — the terse cold-start instruction the agent receives.
  - ``rejected``    — approaches the history rejected; re-proposing any is a miss
                      (re-proposal rate; centri-bench task 1).
  - ``expected_brief`` — substrings that MUST appear in the assembled brief
                      (brief completeness; tasks 1–5).
  - ``superseded``  — (stale, current) fact pairs; the stale one must NOT be in
                      the live brief, the current one must (task 3).
  - ``ground_truth_next`` — the correct next step (next-step correctness).
  - ``dormant_loops`` — loop intents that should be surfaced once when stale
                      (tasks 4, 6).

These are fixed *before* the implementation targets them — the anti-gaming rule
of ``docs/centri-bench.md``. The histories live in the repo as the commitment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple


def _ts(day: int) -> str:
    """A deterministic timestamp ``day`` days into a fixed synthetic project."""
    base = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(days=day, minutes=day * 7)).isoformat()


@dataclass
class Persona:
    key: str
    title: str
    repo_id: str
    events: List[Dict[str, Any]]
    cue: str
    rejected: List[str]
    expected_brief: List[str]
    superseded: List[Tuple[str, str]] = field(default_factory=list)  # (stale, current)
    ground_truth_next: str = ""
    dormant_loops: List[str] = field(default_factory=list)


def _event(eid: str, etype: str, day: int, repo_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": eid, "type": etype, "ts": _ts(day), "source": "bench", "repo_id": repo_id, "payload": payload}


# ----------------------------------------------------------------------
# Persona 1 — trading system (the headline "improve the funding-rate signal")
# ----------------------------------------------------------------------
def _trading() -> Persona:
    repo = "repo-trading"
    events = [
        _event("trd-1", "task.started", 0, repo, {
            "description": "prototype a funding-rate signal for perp basis trades",
            "open_loop": {"id": "loop-kalman", "intent": "try a Kalman filter on the funding-rate signal",
                          "cue": "funding rate signal smoothing"}}),
        _event("trd-2", "task.failed", 2, repo, {
            "description": "raw SMA smoothing of funding rate",
            "summary": "20-period SMA too laggy; signal fired after the move",
            "decision": {"topic": "funding-rate smoothing", "statement": "use a raw 20-period SMA",
                         "stance": "rejected", "rationale": "too laggy, fires after the move"}}),
        _event("trd-3", "task.completed", 5, repo, {
            "description": "EWMA smoothing of funding rate",
            "summary": "EWMA(span=8) tracked turns well in backtest",
            "decision": {"topic": "funding-rate smoothing", "statement": "use EWMA(span=8) smoothing",
                         "stance": "adopted", "rationale": "tracks regime turns without the SMA lag"}}),
        _event("trd-4", "task.completed", 6, repo, {
            "summary": "data source confirmed",
            "fact": {"topic": "funding data source", "statement": "Binance USD-M funding endpoint, 8h cadence"}}),
        _event("trd-5", "task.failed", 9, repo, {
            "description": "leverage the signal at 5x",
            "summary": "5x leverage blew through the risk budget in the 2026-02 drawdown",
            "decision": {"topic": "position sizing", "statement": "size the funding signal at 5x leverage",
                         "stance": "rejected", "rationale": "blew the risk budget in the Feb drawdown"}}),
        _event("trd-6", "task.completed", 12, repo, {
            "summary": "backtester established",
            "fact": {"topic": "backtest harness", "statement": "vectorbt over 2024-2025 funding history"},
            "open_loop": {"id": "loop-walkforward", "intent": "add walk-forward validation to the backtest",
                          "cue": "backtest validation"}}),
        _event("trd-7", "task.completed", 14, repo, {
            "summary": "signal combined with basis",
            "decision": {"topic": "signal blend", "statement": "blend funding EWMA with spot-perp basis z-score",
                         "stance": "adopted", "rationale": "basis confirms funding, cuts false positives"}}),
    ]
    return Persona(
        key="trading",
        title="Perp funding-rate trading signal",
        repo_id=repo,
        events=events,
        cue="improve the funding-rate signal",
        rejected=["raw 20-period SMA", "5x leverage"],
        expected_brief=["EWMA(span=8)", "raw 20-period SMA", "Binance USD-M funding", "blend funding EWMA"],
        ground_truth_next="add walk-forward validation to the backtest",
        dormant_loops=["try a Kalman filter on the funding-rate signal"],
    )


# ----------------------------------------------------------------------
# Persona 2 — web app (stale-fact supersession: a renamed service)
# ----------------------------------------------------------------------
def _webapp() -> Persona:
    repo = "repo-webapp"
    events = [
        _event("web-1", "task.completed", 0, repo, {
            "summary": "auth service named",
            "fact": {"topic": "auth service name", "statement": "the auth service is called authsvc"}}),
        _event("web-2", "task.completed", 1, repo, {
            "decision": {"topic": "session storage", "statement": "store sessions in Redis",
                         "stance": "rejected", "rationale": "adds an ops dependency we want to avoid"}}),
        _event("web-3", "task.completed", 3, repo, {
            "decision": {"topic": "session storage", "statement": "store sessions as signed stateless JWT cookies",
                         "stance": "adopted", "rationale": "no server-side session store to operate"}}),
        _event("web-4", "task.completed", 10, repo, {
            "summary": "auth service renamed",
            "fact": {"topic": "auth service name", "statement": "the auth service is identity-gateway"}}),
        _event("web-5", "task.started", 12, repo, {
            "open_loop": {"id": "loop-ratelimit", "intent": "add rate limiting to the login endpoint",
                          "cue": "login endpoint hardening"}}),
        _event("web-6", "task.completed", 13, repo, {
            "fact": {"topic": "frontend framework", "statement": "React 19 with the app router"}}),
    ]
    return Persona(
        key="webapp",
        title="SaaS web application",
        repo_id=repo,
        events=events,
        cue="harden the login flow",
        rejected=["store sessions in Redis"],
        expected_brief=["identity-gateway", "stateless JWT cookies", "rate limiting"],
        superseded=[("authsvc", "identity-gateway")],
        ground_truth_next="add rate limiting to the login endpoint",
        dormant_loops=["add rate limiting to the login endpoint"],
    )


# ----------------------------------------------------------------------
# Persona 3 — infra migration (procedural conventions + open loops)
# ----------------------------------------------------------------------
def _infra() -> Persona:
    repo = "repo-infra"
    events = [
        _event("inf-1", "task.completed", 0, repo, {
            "fact": {"topic": "deploy convention", "statement": "deploys go out via Terraform plan+apply, never console clicks",
                     "tags": ["convention"]}}),
        _event("inf-2", "task.failed", 2, repo, {
            "decision": {"topic": "migration cutover", "statement": "big-bang cut all services to the new VPC at once",
                         "stance": "rejected", "rationale": "no rollback path; one failure takes everything down"}}),
        _event("inf-3", "task.completed", 4, repo, {
            "decision": {"topic": "migration cutover", "statement": "migrate service-by-service behind a weighted DNS shift",
                         "stance": "adopted", "rationale": "per-service rollback, blast radius contained"}}),
        _event("inf-4", "task.completed", 5, repo, {
            "fact": {"topic": "secrets convention", "statement": "secrets come from Vault at deploy time, never committed",
                     "tags": ["convention"]}}),
        _event("inf-5", "task.started", 7, repo, {
            "open_loop": {"id": "loop-rds", "intent": "migrate the RDS instances to the new VPC",
                          "cue": "database migration"}}),
        _event("inf-6", "task.completed", 9, repo, {
            "summary": "stateless tier migrated",
            "loop_resolution": {"intent": "migrate the stateless web tier", "resolution": "done"},
            "fact": {"topic": "migration progress", "statement": "stateless web tier is on the new VPC; data tier pending"}}),
    ]
    return Persona(
        key="infra",
        title="VPC infrastructure migration",
        repo_id=repo,
        events=events,
        cue="continue the VPC migration",
        rejected=["big-bang cut all services"],
        expected_brief=["service-by-service", "weighted DNS", "Terraform plan+apply", "Vault"],
        ground_truth_next="migrate the RDS instances to the new VPC",
        dormant_loops=["migrate the RDS instances to the new VPC"],
    )


def all_personas() -> List[Persona]:
    return [_trading(), _webapp(), _infra()]
