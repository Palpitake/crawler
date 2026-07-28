"""Authentication evaluation facade used by Browser and Supervisor."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .contracts import build_verification_contract
from .decision import decide_auth_state
from .evidence import collect_auth_evidence
from .models import AuthState, AuthVerificationContract


def evaluate_auth_facts(
    parser: Mapping[str, Any],
    pipeline_info: Optional[Mapping[str, Any]] = None,
    previous: Optional[Mapping[str, Any]] = None,
    *,
    contract: Optional[AuthVerificationContract] = None,
) -> Dict[str, Any]:
    pipeline = pipeline_info if isinstance(pipeline_info, Mapping) else {}
    previous = previous if isinstance(previous, Mapping) else {}
    phases = pipeline.get("phases") if isinstance(pipeline.get("phases"), Mapping) else {}
    login = phases.get("login") if isinstance(phases.get("login"), Mapping) else {}
    contract_value = login.get("verification_contract") if isinstance(login, Mapping) else None
    if contract is None and isinstance(contract_value, Mapping):
        contract = AuthVerificationContract.from_dict(contract_value)
    if contract is None:
        target_url = str(parser.get("target_url") or "")
        contract = build_verification_contract(target_url, parser.get("fields") or [])
    evidence = collect_auth_evidence(parser, pipeline, contract)
    decision = decide_auth_state(
        evidence,
        contract,
        previous_state=AuthState.from_value(previous.get("state")),
    )
    facts = decision.to_facts()
    facts["auth_check_status"] = "completed" if parser else "not_run"
    facts["verification_contract"] = contract.to_dict()
    return facts
