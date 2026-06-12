"""
JSONPatch generation for the mutation webhook.

Given a Pod admission request and the set of ResourceOverride CRs whose selectors
matched the pod's labels, produce the minimal set of JSONPatch operations that bring
the pod's container resources into line with the overrides.

Containers not named in any override are left alone. Containers named in multiple
overrides (a configuration error the validating webhook should reject) are resolved
by 'last write wins' here; we log a warning so the operator can see the conflict in
the audit trail even if the validating webhook didn't reject the offender.
"""
from __future__ import annotations

from dataclasses import dataclass

from src import log as _log_module
from src.webhook_cache import ResourceOverride

_log = _log_module.get(__name__)


# Init containers are intentionally NOT patched. They're short-lived and
# the Prometheus-driven recommender can't produce stable values for them
# (cold-start, ephemeral workloads). The sync loop never includes them on
# a ResourceOverride either (see `src/workload.list_workloads_in_namespace`),
# so this is defence in depth: even if a CR is hand-crafted with an init
# container's name, the webhook won't apply it.
_CONTAINER_PATHS = (
    ("containers", "/spec/containers"),
)


@dataclass(frozen=True)
class PatchOp:
    op: str         # "add" | "replace"
    path: str       # JSONPointer e.g. /spec/containers/0/resources/requests/cpu
    value: object


def _rfc6901_escape(key: str) -> str:
    """Escape a string for use as a single JSONPatch path segment (RFC 6901).

    Order is load-bearing: `~` must be replaced before `/` so a literal `~`
    becomes `~0` and a literal `/` becomes `~1` — escaping `/` first would
    turn `~/` into `~01` instead of the correct `~0~1`. A key with neither
    character (e.g. `kube-resource-updater.applied-from`) is returned as-is.
    """
    return key.replace("~", "~0").replace("/", "~1")


def build_patches(pod: dict, overrides: list[ResourceOverride]) -> list[PatchOp]:
    """Build JSONPatch operations for the pod given a list of matching overrides.

    Returns an empty list when no override applies to any container in the pod.
    The webhook caller is responsible for serialising the patch list into JSON
    and returning it in the AdmissionReview response.
    """
    if not overrides:
        return []

    # Index overrides by container name. Last write wins on conflict.
    by_name: dict[str, dict] = {}
    for ro in overrides:
        for c in ro.containers:
            if c.name in by_name:
                _log.warning(
                    "container %s in pod %s/%s matched by multiple overrides — last wins (current: %s)",
                    c.name, pod.get("metadata", {}).get("namespace"),
                    pod.get("metadata", {}).get("name", "<generated>"), ro.name,
                )
            by_name[c.name] = {
                "requests": dict(c.requests),
                "limits": dict(c.limits),
                "source_cr": f"{ro.namespace}/{ro.name}",
            }

    spec = pod.get("spec") or {}
    patches: list[PatchOp] = []

    for kind, base_path in _CONTAINER_PATHS:
        for idx, container in enumerate(spec.get(kind) or []):
            target = by_name.get(container.get("name"))
            if target is None:
                continue
            patches.extend(_patch_container(container, idx, base_path, target))

    if patches:
        # One annotation per pod records the CRs that contributed; useful for
        # `kubectl describe pod` and audit. The key is passed through
        # `_rfc6901_escape` so any `/` or `~` in a future key produces a valid
        # JSONPatch path; the current key `kube-resource-updater.applied-from`
        # contains neither, so the escaped form is identical.
        sources = sorted({by_name[name]["source_cr"] for name in by_name if name in {
            (c.get("name")) for kind, _ in _CONTAINER_PATHS for c in (spec.get(kind) or [])
        }})
        annotation_value = ",".join(sources)
        # JSONPatch add at `/metadata/annotations/<key>` requires the parent
        # object `/metadata/annotations` to exist. Pods with NO annotations
        # at all (StatefulSet templates, raw manifests) lack the parent
        # entirely and the apiserver rejects the patch with "doc is missing
        # path: ...: missing value", blocking pod creation. Detect that
        # case and add the whole annotations object atomically instead.
        existing_annotations = (pod.get("metadata") or {}).get("annotations")
        if not existing_annotations:
            patches.append(PatchOp(
                op="add",
                path="/metadata/annotations",
                value={"kube-resource-updater.applied-from": annotation_value},
            ))
        else:
            patches.append(PatchOp(
                op="add",
                path="/metadata/annotations/" + _rfc6901_escape("kube-resource-updater.applied-from"),
                value=annotation_value,
            ))

    return patches


def _patch_container(container: dict, idx: int, base_path: str, target: dict) -> list[PatchOp]:
    """Patches for one container's resources.

    Strategy:
      - If the container has no `resources` field at all, add one whole node.
      - Otherwise, replace `requests` and `limits` sub-trees individually so we
        don't clobber other resource fields (e.g. extended resources, nvidia.com/gpu)
        that the operator may have set intentionally.
    """
    out: list[PatchOp] = []
    resources = container.get("resources")
    container_path = f"{base_path}/{idx}/resources"

    requests = target["requests"]
    limits = target["limits"]
    if not requests and not limits:
        return out

    if resources is None:
        # First touch: add the whole node atomically.
        node: dict[str, dict] = {}
        if requests:
            node["requests"] = requests
        if limits:
            node["limits"] = limits
        out.append(PatchOp(op="add", path=container_path, value=node))
        return out

    # Existing resources node — replace sub-trees individually.
    if requests:
        if "requests" in resources:
            out.append(PatchOp(op="replace", path=f"{container_path}/requests", value=requests))
        else:
            out.append(PatchOp(op="add", path=f"{container_path}/requests", value=requests))
    if limits:
        if "limits" in resources:
            out.append(PatchOp(op="replace", path=f"{container_path}/limits", value=limits))
        else:
            out.append(PatchOp(op="add", path=f"{container_path}/limits", value=limits))
    return out


def patches_to_jsonpatch(patches: list[PatchOp]) -> list[dict]:
    """Serialise PatchOp list to the dict form expected in an AdmissionReview response."""
    return [{"op": p.op, "path": p.path, "value": p.value} for p in patches]


def applied_source_crs(pod: dict, overrides: list[ResourceOverride]) -> set[str]:
    """Return the `"namespace/name"` set of CRs that actually contributed
    container patches for this pod admission.

    Rationale: `build_patches` filters two ways internally — last-wins on
    duplicate container names within `overrides`, AND skips CRs whose
    container list shares no name with the pod's `spec.containers`. The
    `applied-from` annotation already exposes this post-filter set in the
    patch output, but webhook_server.py also needs it (without re-running
    the full patch build) to stamp `status.lastAppliedAt` only on CRs
    that truly applied — not every selector-matched CR.

    Mirrors the logic at `build_patches:81-83` so the two stay in lock-step.
    """
    if not overrides:
        return set()
    spec = pod.get("spec") or {}
    pod_container_names: set[str] = {
        (c.get("name") or "")
        for _kind, _base in _CONTAINER_PATHS
        for c in (spec.get(_kind) or [])
    }
    if not pod_container_names:
        return set()
    # Last-wins on duplicate container names — same iteration order as
    # build_patches so the winner is identical.
    winner_cr: dict[str, str] = {}
    for ro in overrides:
        for c in ro.containers:
            if c.name in pod_container_names:
                winner_cr[c.name] = f"{ro.namespace}/{ro.name}"
    return set(winner_cr.values())


def patched_containers_per_cr(
    pod: dict, overrides: list[ResourceOverride],
) -> dict[str, frozenset[str]]:
    """Map `"namespace/name"` → frozenset of container names that actually
    contributed a patch for this pod admission.

    Same last-wins tournament as `applied_source_crs` / `build_patches` (kept
    in this module so the three stay in lock-step if `_CONTAINER_PATHS`
    changes). Used by the status writer to stamp `status.patchedContainers`.
    Empty dict when no override has any container in the pod.
    """
    if not overrides:
        return {}
    spec = pod.get("spec") or {}
    pod_container_names: set[str] = {
        (c.get("name") or "")
        for _kind, _base in _CONTAINER_PATHS
        for c in (spec.get(_kind) or [])
    }
    if not pod_container_names:
        return {}
    winner_cr: dict[str, str] = {}
    for ro in overrides:
        for c in ro.containers:
            if c.name in pod_container_names:
                winner_cr[c.name] = f"{ro.namespace}/{ro.name}"
    result: dict[str, frozenset[str]] = {}
    for container_name, cr_id in winner_cr.items():
        result[cr_id] = result.get(cr_id, frozenset()) | {container_name}
    return result
