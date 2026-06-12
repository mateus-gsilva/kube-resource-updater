"""
Namespace-based opt-in discovery.

The sync loop's entry point: list every Namespace carrying
`kube-resource-updater.enabled: "true"` as an annotation (no labels —
see ROADMAP "Annotation-only namespace-based opt-in" for why), bundle each
with its annotations dict so the resolver can apply per-namespace overrides
to the workload-level config later.

This module replaces the entire ArgoCD `Application` discovery layer that
the tool used to depend on. After this refactor the tool no longer reads
from `argoproj.io/v1alpha1` resources at all — runs equally well on any
GitOps stack (Argo CD, Flux, kustomize-controller, plain kubectl).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kubernetes import client
from kubernetes.client.rest import ApiException

from . import log as _log_module
from .overrides import is_namespace_enabled

_log = _log_module.get(__name__)


@dataclass
class NamespaceTarget:
    """A namespace that has opted in to the tool.

    Carries the verbatim annotations dict; the resolver in `src/overrides.py`
    is responsible for filtering down to recognised keys and turning the
    string values into typed config overrides.
    """
    name: str
    annotations: dict[str, str] = field(default_factory=dict)


def list_enabled_namespaces(
    core_api: client.CoreV1Api | None = None,
) -> list[NamespaceTarget]:
    """Return every Namespace with the opt-in annotation set to a truthy value.

    `kubernetes.io/metadata.name == kube-system` is excluded as defence in
    depth — the operator should never opt that one in, but if they do by
    accident we still skip it. Other system / platform namespaces
    (`kube-public`, `default`, the release namespace itself) are not
    excluded; if you mark them as enabled, we trust the choice.

    Transient API failures (5xx, timeouts, watch reset) are swallowed
    with a warning — the sync run finds no namespaces, exits clean, the
    next CronJob fire retries. Better than crashing on a blip.

    Auth failures (401/403) are NOT swallowed: a 401 means the bearer
    token is missing or rejected (the kubernetes-client 36.0.0 in-cluster
    auth regression of May 2026 surfaced this way — `auth_settings()`
    returned empty so no Authorization header was sent, every list call
    got 401, the swallow turned that into a clean exit-0 with no work
    and masked the bug for ~24h). A 403 means the SA / RBAC binding is
    misconfigured. Both deserve to fail the Job loud so the next
    CronJob alert fires.
    """
    api = core_api or client.CoreV1Api()
    try:
        items = api.list_namespace().items
    except ApiException as exc:
        if exc.status in (401, 403):
            _log.error(
                "apiserver rejected list-namespaces (%s %s) — failing the Job. "
                "Likely SA-token / RBAC / client-library issue, not a transient blip.",
                exc.status, exc.reason,
            )
            raise
        _log.warning("Failed to list namespaces: %s", exc)
        return []
    except Exception as exc:
        _log.warning("Failed to list namespaces: %s", exc)
        return []

    out: list[NamespaceTarget] = []
    for ns in items:
        meta = ns.metadata
        if not meta or meta.name == "kube-system":
            continue
        annotations = dict(meta.annotations or {})
        if not is_namespace_enabled(annotations):
            # DEBUG (was INFO until 1.22.9): a cluster with 100 namespaces
            # produces 95+ "not opted in" lines per CronJob run, drowning
            # out the actually-actioning log. Operators debugging "why
            # isn't namespace X being worked?" can re-run with
            # `--log-level DEBUG` to see the breadcrumb.
            _log.debug("[discovery] namespace %s not opted in, skipping", meta.name)
            continue
        out.append(NamespaceTarget(name=meta.name, annotations=annotations))

    _log.debug(
        "[discovery] %d namespace(s) opted in: %s",
        len(out), ", ".join(t.name for t in out) or "(none)",
    )
    return out
