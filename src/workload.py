"""Workload discovery and resource recommendation data structures.

The tool reads workloads (Deployments, StatefulSets, DaemonSets, and CronJobs)
directly from the local cluster's k8s API, scoped to namespaces that opted in
via the `kube-resource-updater.enabled: "true"` annotation. CronJob discovery
requires a `BatchV1Api` instance in addition to `AppsV1Api`. Standalone Jobs
are intentionally excluded — see the `list_workloads_in_namespace` docstring
for the rationale. Multi-cluster fan-out and the ArgoCD `Application`
discovery layer were removed in the same refactor as the annotation-only
opt-in (see ROADMAP).
"""

from dataclasses import dataclass, field

from kubernetes import client

from . import log as _log_module

_log = _log_module.get(__name__)


@dataclass
class ResourceRequest:
    cpu: str | None = None
    memory: str | None = None


@dataclass
class ContainerRecommendation:
    container_name: str
    target: ResourceRequest = field(default_factory=ResourceRequest)
    lower_bound: ResourceRequest = field(default_factory=ResourceRequest)
    upper_bound: ResourceRequest = field(default_factory=ResourceRequest)
    uncapped_target: ResourceRequest = field(default_factory=ResourceRequest)


@dataclass
class WorkloadRecommendation:
    name: str
    namespace: str
    target_kind: str
    target_name: str
    containers: list[ContainerRecommendation] = field(default_factory=list)
    helm_release: str = ""           # `app.kubernetes.io/instance` label — empty for non-helm workloads
    annotations: dict[str, str] = field(default_factory=dict)  # workload-level overrides
    # Snapshot of the workload's PodTemplate labels. Used by the writeback
    # layer to derive a selector unique enough that the admission webhook
    # never matches more than one CR per pod. We capture the **template**
    # labels (not the workload's own metadata.labels) because those are
    # what the pods inherit and what the CR's matchLabels run against.
    pod_template_labels: dict[str, str] = field(default_factory=dict)
    # Init container names captured at discovery for log visibility — the
    # actual filter happens implicitly because `containers` only holds the
    # regular spec.containers list (init containers were never written to
    # CRs in the first place). Kept here so the sync log can say
    # "skipping init: <names>" for operators auditing the filter.
    init_container_names: list[str] = field(default_factory=list)
    # Per-workload skipContainers list resolved through the
    # helm < namespace < workload chain. Filled in by main.cmd_sync after
    # discovery; the writeback layer reads it to drop the named regular
    # containers from the generated CR.
    skip_containers: list[str] = field(default_factory=list)

    @property
    def has_recommendation(self) -> bool:
        return len(self.containers) > 0


def _tmpl_spec_template(item):
    """Standard pod-template path: item.spec.template (Deployment/StatefulSet/DaemonSet)."""
    return item.spec.template if item.spec else None


def _tmpl_cronjob(item):
    """CronJob pod-template path: item.spec.job_template.spec.template (two levels deep).

    NOTE: the k8s python client attribute is `job_template` (snake_case),
    not the JSON `jobTemplate` — accessing the wrong name would AttributeError
    and the CronJob would be silently dropped by the per-kind except.
    """
    jt = item.spec.job_template if item.spec else None
    jt_spec = jt.spec if jt else None
    return jt_spec.template if jt_spec else None


def _skip_paused(item) -> bool:
    """True when the item is a paused Deployment (spec.paused=true)."""
    return bool(getattr(item.spec, "paused", False))


def _skip_suspended(item) -> bool:
    """True when the item is a suspended CronJob (spec.suspend=true)."""
    return bool(getattr(item.spec, "suspend", False))


def _skip_never(_item) -> bool:
    return False


def list_workloads_in_namespace(
    apps_api: client.AppsV1Api,
    namespace: str,
    batch_api: "client.BatchV1Api | None" = None,
) -> list[WorkloadRecommendation]:
    """Discover Deployments, StatefulSets, DaemonSets, and CronJobs in a
    single namespace.

    Each kind carries its own template accessor and skip predicate so the
    loop body stays uniform:

      - Deployments/StatefulSets/DaemonSets: pod template at
        ``item.spec.template``; skip predicate is ``spec.paused`` (only
        meaningful for Deployments).
      - CronJobs: pod template at ``item.spec.job_template.spec.template``
        (two levels deep relative to the CronJob root); skip predicate is
        ``spec.suspend``.

    Standalone Jobs are intentionally NOT discovered. A Job is ephemeral
    (typically complete before the next sync tick), CRs apply at pod
    admission (so the only running pod started before any sync saw it), and
    CronJob-owned Jobs are covered transitively: the CronJob CR's
    ``matchLabels`` is derived from the jobTemplate pod-template labels,
    which propagate verbatim to every Job the CronJob spawns.

    Skips workloads with no containers. Annotations are stored verbatim —
    the resolver in ``src/overrides.py`` filters down to the
    ``kube-resource-updater.*`` keys and validates types.

    ``batch_api`` is optional so apps-only callers (and tests) keep working;
    when ``None``, CronJob discovery is skipped with a per-namespace warning.
    """
    _KINDS: list[tuple[str, object, object, object]] = [
        ("Deployment",  apps_api.list_namespaced_deployment,    _tmpl_spec_template, _skip_paused),
        ("StatefulSet", apps_api.list_namespaced_stateful_set,  _tmpl_spec_template, _skip_never),
        ("DaemonSet",   apps_api.list_namespaced_daemon_set,    _tmpl_spec_template, _skip_never),
    ]
    if batch_api is not None:
        _KINDS.append(
            ("CronJob", batch_api.list_namespaced_cron_job, _tmpl_cronjob, _skip_suspended),
        )
    else:
        _log.warning(
            "[discovery] batch_api not provided — CronJob discovery skipped for namespace %s",
            namespace,
        )

    results: list[WorkloadRecommendation] = []
    for kind, list_fn, tmpl_accessor, skip_pred in _KINDS:
        try:
            for item in list_fn(namespace).items:
                # Paused Deployments / suspended CronJobs should not get a CR —
                # emitting one lets the webhook override that intent on the
                # next pod creation. (Audit framework Area 2.)
                if skip_pred(item):
                    _log.info(
                        "[discovery] %s/%s/%s is paused/suspended — skipping",
                        kind, namespace, getattr(item.metadata, "name", "<unknown>"),
                    )
                    continue
                tmpl = tmpl_accessor(item)
                tmpl_spec = tmpl.spec if tmpl else None
                containers = [
                    ContainerRecommendation(container_name=c.name)
                    for c in (tmpl_spec.containers if tmpl_spec else []) or []
                ]
                if not containers:
                    continue
                # Init containers are intentionally NOT included in
                # `containers` — they're short-lived and benefit nothing
                # from a Prometheus-driven recommendation. We still
                # capture their names so the sync log can advertise
                # which ones were skipped.
                init_names = [
                    c.name for c in (tmpl_spec.init_containers if tmpl_spec else []) or []
                ]
                if init_names:
                    _log.info(
                        "[discovery] %s/%s/%s skipping init containers: %s",
                        kind, namespace, item.metadata.name, ", ".join(init_names),
                    )
                labels = item.metadata.labels or {}
                annotations = item.metadata.annotations or {}
                release = labels.get("app.kubernetes.io/instance", "")
                # PodTemplate labels — what the pods inherit and what the
                # CR's selector.matchLabels will be tested against at
                # admission time. May differ from the workload's own
                # metadata.labels (e.g. component label may be added only
                # at the template level by some Helm charts).
                tmpl_meta = (tmpl.metadata if tmpl else None)
                pod_template_labels = (tmpl_meta.labels if tmpl_meta else None) or {}
                results.append(WorkloadRecommendation(
                    name=item.metadata.name,
                    namespace=namespace,
                    target_kind=kind,
                    target_name=item.metadata.name,
                    containers=containers,
                    helm_release=release,
                    annotations=dict(annotations),
                    pod_template_labels=dict(pod_template_labels),
                    init_container_names=init_names,
                ))
        except Exception as exc:
            _log.warning("Failed to list %ss in %s: %s", kind, namespace, exc)
    return results


def list_workloads(
    apps_api: client.AppsV1Api,
    namespaces: list[str],
    batch_api: "client.BatchV1Api | None" = None,
) -> list[WorkloadRecommendation]:
    """Discover workloads across many namespaces in one call.

    Deduplicates the namespace list — the upstream caller may have built
    it from a Namespace list that included repeats from informer cache
    refreshes. Order is preserved for log readability.

    Pass ``batch_api`` to include CronJob discovery; omitted → CronJobs
    skipped with a per-namespace warning.
    """
    seen: set[str] = set()
    out: list[WorkloadRecommendation] = []
    for ns in namespaces:
        if ns in seen:
            continue
        seen.add(ns)
        out.extend(list_workloads_in_namespace(apps_api, ns, batch_api=batch_api))
    return out
