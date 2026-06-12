"""
Validating admission webhook for `ResourceOverride` CRs.

Catches the operator-actionable failure mode the mutating webhook
treats as "last write wins" silently: two CRs in the same namespace
whose selectors could match the same pod AND that share a container
name. With the validator on, the apiserver rejects the second CR at
apply time with a clear error instead of leaving a `[mutate] container
<name> matched by multiple overrides` warning in the runtime log.

The 1.4.1 selector inference fix made overlap unlikely for tool-
generated CRs (selector now uses `instance + name + component`).
The validator guards against:
  - hand-edited CRs with a copied selector
  - third-party tools that create CRs concurrently
  - future regressions that broaden the selector again

Off by default — enable cluster-wide via `webhook.validating.enabled`
(see chart values). The ValidatingWebhookConfiguration's failurePolicy
is `Ignore`, so a webhook outage never blocks `kubectl apply`.
"""
from __future__ import annotations

from typing import Any

from src import log as _log_module
from src.webhook_cache import ResourceOverride, ResourceOverrideCache

_log = _log_module.get(__name__)


# --------------------------------------------------------------------------- #
# Pure logic (selector + container overlap)                                    #
# --------------------------------------------------------------------------- #

def _selectors_can_overlap(a: dict[str, str], b: dict[str, str]) -> bool:
    """True iff some pod could carry every key/value of both selectors.

    Two `matchLabels` selectors can match the same pod iff every key
    they share has the same value. A key in only one selector imposes
    no constraint on the other side. Empty selectors return False —
    the cache treats empty `matchLabels` as a no-op (see
    `_parse` in `src/webhook_cache.py`), so we do too here.
    """
    if not a or not b:
        return False
    for k, va in a.items():
        if k in b and b[k] != va:
            return False
    return True


def _container_names_intersect(a: tuple, b: tuple) -> set[str]:
    """Container names that appear in BOTH override lists. Empty when
    no container would receive conflicting patches even if the pod
    matched both selectors.
    """
    return {c.name for c in a} & {c.name for c in b}


def _find_conflicts(
    new_ro: ResourceOverride,
    siblings: list[ResourceOverride],
) -> list[tuple[str, set[str]]]:
    """Return the list of (sibling CR name, conflicting container names) for
    every sibling whose selector AND container set overlaps with new_ro.

    Self-exclusion: the cache may already hold a previous version of
    new_ro (UPDATE flow). Filter by (namespace, name); a CR is never
    considered to conflict with its own previous revision.
    """
    out: list[tuple[str, set[str]]] = []
    for sib in siblings:
        if sib.namespace == new_ro.namespace and sib.name == new_ro.name:
            continue
        if not _selectors_can_overlap(new_ro.selector_match_labels,
                                      sib.selector_match_labels):
            continue
        shared = _container_names_intersect(new_ro.containers, sib.containers)
        if shared:
            out.append((sib.name, shared))
    return out


# --------------------------------------------------------------------------- #
# AdmissionReview wiring                                                       #
# --------------------------------------------------------------------------- #

def _allow(uid: str, message: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"uid": uid, "allowed": True}
    if message:
        response["warnings"] = [f"resource-updater-validating: {message}"]
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": response,
    }


allow = _allow  # public alias — callers outside this module use webhook_validate.allow(...)


def _deny(uid: str, message: str) -> dict[str, Any]:
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": False,
            "status": {
                "code": 422,
                "reason": "Invalid",
                "message": message,
            },
        },
    }


def validate(
    review: dict[str, Any],
    cr_cache: ResourceOverrideCache,
) -> dict[str, Any]:
    """Compute an AdmissionResponse for a CREATE/UPDATE on a ResourceOverride.

    Reads the candidate CR from `review.request.object`, gathers siblings
    from the cache (same namespace, excluding self), and rejects with a
    clear `kubectl apply` error message when an overlap is found.

    Robustness: any parse error or cache-unready situation falls back to
    `allow` — same shape as the mutating webhook. The
    ValidatingWebhookConfiguration uses `failurePolicy: Ignore` so a
    bad response also degrades to "allow" by the apiserver's own
    contract.
    """
    req = review.get("request") or {}
    uid = req.get("uid", "")
    obj = req.get("object") or {}

    if not cr_cache.ready():
        return _allow(uid, "validator cache unready, skipping overlap check")

    # Selector validation — two failure modes rejected at admission:
    #
    # (a) matchExpressions present (with or without matchLabels):
    #     The cache's `_parse` only reads `matchLabels`; any
    #     `matchExpressions` constraints are silently dropped, making the
    #     effective selector broader than the operator intended. This is a
    #     correctness bug — pod mutations fire on workloads the CR did not
    #     mean to cover. Deny with an explicit message pointing at the
    #     unsupported field. (Bug #40.)
    #
    # (b) matchLabels absent/empty (no matchExpressions either):
    #     The CR would target every pod in the namespace (covered now by
    #     the matches() defensive check, but pre-1.21.0 it actually
    #     fired). The cache's `_parse` silently drops these CRs — they
    #     end up in etcd but never patch anything. Operator-surprising:
    #     `kubectl apply` succeeds, `kubectl get ro` shows the CR, but no
    #     pod ever gets patched. Reject at admission for immediate,
    #     actionable feedback. (Audit v2 finding D5.)
    spec = (obj.get("spec") or {})
    selector = spec.get("selector") or {}
    match_labels = selector.get("matchLabels") or {}
    match_expressions = selector.get("matchExpressions") or []
    ns = (obj.get("metadata") or {}).get("namespace", "<unknown>")
    nm = (obj.get("metadata") or {}).get("name", "<unknown>")

    if match_expressions:
        # Sub-case (a): matchExpressions present — deny regardless of
        # whether matchLabels is also set, because the dropped expressions
        # would make the effective selector silently broader than written.
        return _deny(
            uid,
            f"ResourceOverride {ns}/{nm} uses spec.selector.matchExpressions, "
            "which is not supported — only matchLabels is evaluated at admission "
            "and at patch time. The matchExpressions constraints would be silently "
            "ignored, making the selector broader than intended. "
            "Express the constraint via matchLabels key/value pairs, or split "
            "into multiple ResourceOverride CRs."
        )

    if not match_labels:
        # Sub-case (b): neither matchLabels nor matchExpressions — empty
        # selector that would match every pod in the namespace.
        return _deny(
            uid,
            f"ResourceOverride {ns}/{nm} has empty spec.selector.matchLabels — "
            "this would target every pod in the namespace. Set at least one "
            "label (e.g. app.kubernetes.io/instance) so the override applies "
            "to a specific workload."
        )

    # Parse the candidate the same way the cache does so we compare
    # against the same shape. _parse already filters empty selectors
    # (handled above with a hard deny) and CRs with no containers
    # (handled by the apiserver's CRD schema validation).
    from src.webhook_cache import _parse  # local import to dodge any cycles
    candidate = _parse(obj)
    if candidate is None:
        # Malformed in some other way (no containers, missing fields).
        # The CRD's OpenAPI schema will catch most of these. Allow here
        # — failing inside the validating webhook would shadow the CRD
        # error message which is more specific.
        return _allow(uid)

    # Walk the cache's siblings. lookup() returns CRs whose selector
    # matches a label dict; we want EVERY CR in the namespace, regardless
    # of selector, to check overlap. So pull from the underlying
    # NamespaceIndex via lookup with a wildcard call — instead, reach
    # for the index directly through the public `_index` (single
    # underscore is intentional to keep the surface small but accessible
    # from inside the package).
    with cr_cache._lock:
        ns_idx = cr_cache._index.get(candidate.namespace)
        siblings = list(ns_idx.overrides.values()) if ns_idx else []

    conflicts = _find_conflicts(candidate, siblings)
    if not conflicts:
        return _allow(uid)

    parts = [
        f"{name} (containers: {', '.join(sorted(cs))})"
        for name, cs in conflicts
    ]
    msg = (
        f"ResourceOverride {candidate.namespace}/{candidate.name} has selector "
        f"+ container overlap with existing CR(s): {'; '.join(parts)}. "
        f"Either narrow the selector (e.g. add app.kubernetes.io/component) or "
        f"remove the conflicting container entries."
    )
    _log.info("[validate] rejected %s/%s: %s", candidate.namespace, candidate.name, msg)
    return _deny(uid, msg)
