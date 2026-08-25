"""Deterministic authorization for typed infrastructure operations.

This module deliberately contains no environment names, adapters, credentials,
or provider/model identities.  Adapters may implement an operation, but only
this policy plus the configured worker/environment intersection may authorize
it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class InfrastructureRisk(str, Enum):
    READ = "READ"
    MUTATING_REVERSIBLE = "MUTATING_REVERSIBLE"
    MUTATING_DISRUPTIVE = "MUTATING_DISRUPTIVE"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass(frozen=True)
class InfrastructureOperationPolicy:
    operation: str
    risk: InfrastructureRisk
    agent_allowed: bool = True


OPERATIONS: dict[str, InfrastructureOperationPolicy] = {
    "inspect_host": InfrastructureOperationPolicy("inspect_host", InfrastructureRisk.READ),
    "list_vms": InfrastructureOperationPolicy("list_vms", InfrastructureRisk.READ),
    "inspect_vm": InfrastructureOperationPolicy("inspect_vm", InfrastructureRisk.READ),
    "inspect_service": InfrastructureOperationPolicy("inspect_service", InfrastructureRisk.READ),
    "start_vm": InfrastructureOperationPolicy("start_vm", InfrastructureRisk.MUTATING_REVERSIBLE),
    "restart_vm": InfrastructureOperationPolicy("restart_vm", InfrastructureRisk.MUTATING_DISRUPTIVE),
    # Retained as an explicit denied taxonomy entry; it is not an executable tool.
    "delete_vm": InfrastructureOperationPolicy("delete_vm", InfrastructureRisk.DESTRUCTIVE, False),
}


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    code: str | None
    risk: InfrastructureRisk | None
    authorization_source: str | None


def operation_policy(operation: str) -> InfrastructureOperationPolicy | None:
    return OPERATIONS.get(operation)


def authorize(operation: str, worker_capabilities: set[str], environment_capabilities: set[str], config: dict[str, Any]) -> AuthorizationDecision:
    policy = operation_policy(operation)
    if policy is None or not policy.agent_allowed:
        return AuthorizationDecision(False, "operation_not_allowed" if policy is None else "destructive_operation_disabled", policy.risk if policy else None, None)
    if operation not in worker_capabilities or operation not in environment_capabilities:
        return AuthorizationDecision(False, "capability_missing", policy.risk, None)
    if policy.risk is InfrastructureRisk.DESTRUCTIVE:
        return AuthorizationDecision(False, "destructive_operation_disabled", policy.risk, None)
    if policy.risk is InfrastructureRisk.READ:
        return AuthorizationDecision(True, None, policy.risk, "read_policy")
    risks = config.get("allowed_risk_classes", [])
    if not isinstance(risks, list) or policy.risk.value not in risks:
        return AuthorizationDecision(False, "risk_not_authorized", policy.risk, None)
    if policy.risk is InfrastructureRisk.MUTATING_DISRUPTIVE:
        operations = config.get("preauthorized_operations", [])
        if not isinstance(operations, list) or operation not in operations:
            return AuthorizationDecision(False, "risk_not_authorized", policy.risk, None)
    return AuthorizationDecision(True, None, policy.risk, "environment_preauthorization")
