"""
In-memory caches kept in sync via the Kubernetes watch API.

Two caches live here:

  - `ResourceOverrideCache` — every `ResourceOverride` CR in the cluster, indexed by
     namespace. Walked on every Pod admission to find selectors that match the pod.
  - `NamespaceCache` — every `Namespace` in the cluster, with its annotations. Used by
     the webhook to short-circuit admission for namespaces that didn't opt in
     (`kube-resource-updater.enabled: "true"` annotation).

Both consult an in-memory snapshot at admission time. Reading from the live API would
put admission latency at the mercy of the API server's queue depth and would multiply
admission load onto kube-apiserver — neither acceptable inside a 5s admission timeout.

Watch reconnect is handled by restarting the watch loop with the latest resourceVersion
on any expected k8s exception (410 Gone, 504 Timeout, EOF, network reset). Unexpected
exceptions kill the loop so the Pod's liveness probe surfaces the failure.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from collections.abc import Callable, Iterable

from kubernetes import client, watch

from src import log as _log_module
from src.overrides import is_namespace_enabled

_log = _log_module.get(__name__)


# Capped-exponential backoff helper, used by both
# `ResourceOverrideCache` and `NamespaceCache` on watch-reconnect AND
# during bootstrap retry. Full-jitter (50%-100% of base) so two webhook
# replicas hitting an etcd-compaction RV-410 at the same time don't
# retry in lockstep against the same overloaded apiserver
# (multi-replica behaviour).
def _backoff_sleep(stop: threading.Event, attempt: int,
                   initial: float, maximum: float) -> bool:
    """Sleep with full-jitter exponential backoff; returns True if `stop` fired.

    `attempt` is 0-indexed: the first call (attempt=0) sleeps roughly `initial`
    seconds, doubling per attempt up to `maximum`. Jitter scales the resulting
    base by a uniform factor in [0.5, 1.0).
    """
    base = min(initial * (2 ** attempt), maximum)
    delay = base * (0.5 + random.random() * 0.5)
    return stop.wait(delay)


# After this many consecutive `_initial_list` failures during a reconnect
# storm, the cache flips `_ready` to False so `/readyz` returns 503 and
# the kubelet pulls the webhook pod out of Service endpoints. K=3 means
# ~1+2+4=7s of trouble before degrading — fast enough that
# `failurePolicy: Ignore` doesn't admit too many un-mutated pods, slow
# enough that a single transient blip doesn't depage on-call.
_READY_CLEAR_AFTER_K = 3


CRD_GROUP = "kube-resource-updater.io"
CRD_VERSION = "v1"
CRD_PLURAL = "resourceoverrides"


@dataclass(frozen=True)
class ContainerOverride:
    """Resolved per-container override pulled out of a CR for fast admission lookup."""
    name: str
    requests: dict[str, str]   # {"cpu": "100m", "memory": "256Mi"}
    limits: dict[str, str]


@dataclass(frozen=True)
class ResourceOverride:
    """Snapshot of a single ResourceOverride CR. Immutable; cache replaces on update."""
    namespace: str
    name: str
    selector_match_labels: dict[str, str]
    containers: tuple[ContainerOverride, ...]

    def matches(self, pod_labels: dict[str, str]) -> bool:
        """True iff the pod's labels satisfy every key/value in the selector.

        matchExpressions are not supported in this initial cut; matchLabels is sufficient
        for the workloads the tool currently targets. Adding matchExpressions later is a
        local change to this method.

        Empty selector → no-match (NOT match-everything). The vacuous truth
        path of `for ... in {}: return True` was a real bug: a hand-edited CR with `matchLabels: {}` would have
        the cache return True for every pod in the namespace and the
        mutation webhook patch every pod's resources to the CR's values.
        The validating webhook's `_selectors_can_overlap` ALREADY treats
        empty as no-overlap; the mutation path now agrees. Empty CRs are
        also rejected at admission by the validator (defence in depth).
        """
        if not self.selector_match_labels:
            return False
        for k, v in self.selector_match_labels.items():
            if pod_labels.get(k) != v:
                return False
        return True


@dataclass
class _NamespaceIndex:
    overrides: dict[str, ResourceOverride] = field(default_factory=dict)


class ResourceOverrideCache:
    """Thread-safe in-memory cache of ResourceOverride CRs across all namespaces.

    Public surface:
      start()           — begin the background watch goroutine; non-blocking.
      stop()            — signal the watch goroutine to exit; idempotent.
      lookup(ns, labels) — return all overrides in `ns` whose selector matches `labels`.
      ready()           — True once the initial list has populated the cache.
    """

    def __init__(
        self,
        api: client.CustomObjectsApi,
        event_callback: Callable[[str, ResourceOverride, ResourceOverride | None], None] | None = None,
    ):
        self._api = api
        self._index: dict[str, _NamespaceIndex] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._resource_version: str | None = None
        # Optional hook, fired on every ADDED/MODIFIED/DELETED watch
        # event. Receives (event_type, new_ro, prev_ro). Used by the
        # rollout trigger; left None when the controller surface is off.
        self._event_callback = event_callback

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="resource-override-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def ready(self) -> bool:
        return self._ready.is_set()

    # ------------------------------------------------------------------ #
    # Read path (admission)                                                #
    # ------------------------------------------------------------------ #

    def lookup(self, namespace: str, pod_labels: dict[str, str]) -> list[ResourceOverride]:
        """Return all overrides in `namespace` whose selector matches `pod_labels`.

        Returns an empty list when the namespace has no overrides or none match.
        Multiple matches are possible; callers must define ordering / conflict policy.
        """
        with self._lock:
            ns_idx = self._index.get(namespace)
            if ns_idx is None:
                return []
            return [ro for ro in ns_idx.overrides.values() if ro.matches(pod_labels)]

    # ------------------------------------------------------------------ #
    # Watch loop                                                           #
    # ------------------------------------------------------------------ #

    # Bootstrap retry backoff. Class attributes so tests can override via
    # `patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_INITIAL_S", 0.01)`.
    _BOOTSTRAP_BACKOFF_INITIAL_S = 1.0
    _BOOTSTRAP_BACKOFF_MAX_S = 60.0

    def _run(self) -> None:
        # Initial list bootstrap. Pre-fix this was a single try/except that
        # returned on failure — killing the daemon thread permanently if the
        # apiserver was briefly unreachable (transient 5xx, RBAC propagation
        # lag during pod start). `_ready` would never set, `/readyz` would
        # stay 503 forever, and the webhook served zero patches under
        # failurePolicy: Ignore. Now we retry with capped exponential
        # backoff, keyed on `_stop` so `stop()` still terminates promptly.
        backoff = self._BOOTSTRAP_BACKOFF_INITIAL_S
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                self._initial_list()
                break
            except Exception:
                _log.exception(
                    "resource-override watch bootstrap failed (attempt %d); "
                    "retrying in %.1fs", attempt, backoff,
                )
                if self._stop.wait(backoff):
                    return  # `stop()` was called during backoff
                backoff = min(backoff * 2, self._BOOTSTRAP_BACKOFF_MAX_S)
        else:
            return  # stop() raced with the loop start; nothing to do

        # Reconnect uses capped-exponential backoff (was fixed
        # 2.0s sleep until 1.22.10). Tracks consecutive `_initial_list`
        # failures; after K=`_READY_CLEAR_AFTER_K` in a row, `_ready.clear()`
        # so `/readyz` flips to 503 and kubelet drops the pod from the
        # Service endpoints. On the first successful reconnect,
        # `_initial_list` re-sets `_ready`.
        consecutive_fail = 0
        while not self._stop.is_set():
            try:
                self._watch_loop()
                consecutive_fail = 0
            except Exception:
                # Re-list from scratch on any unexpected error. The watch API itself
                # signals 410 Gone via a normal stream end; we re-list with the new RV.
                _log.warning("resource-override watch interrupted; re-listing", exc_info=True)
                try:
                    self._initial_list()
                    consecutive_fail = 0
                except Exception:
                    consecutive_fail += 1
                    _log.exception(
                        "resource-override re-list failed (consecutive=%d); "
                        "will retry with backoff",
                        consecutive_fail,
                    )
                    if consecutive_fail >= _READY_CLEAR_AFTER_K:
                        self._ready.clear()
                    if _backoff_sleep(self._stop, consecutive_fail - 1,
                                      self._BOOTSTRAP_BACKOFF_INITIAL_S,
                                      self._BOOTSTRAP_BACKOFF_MAX_S):
                        return

    def _initial_list(self) -> None:
        """List all CRs and replace the cache.

        Two callers: bootstrap (cache empty) and watch-reconnect (cache
        populated, watch stream broke and we need to recover from the
        latest RV in etcd). On RECONNECT we diff old vs new and fire
        synthetic ADDED/MODIFIED/DELETED callbacks for anything that
        changed during the watch outage. Without this synthesis, watch
        events that landed during the gap would be silently dropped —
        the rollout trigger and any other callback consumer would never
        learn about them. (Bug surfaced when ArgoCD-driven CR updates
        coincided with a watch reconnect.)
        """
        result = self._api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL,
        )
        self._resource_version = result.get("metadata", {}).get("resourceVersion")
        new_flat: dict[tuple[str, str], ResourceOverride] = {}
        new_index: dict[str, _NamespaceIndex] = {}
        for item in result.get("items", []):
            ro = _parse(item)
            if ro is None:
                continue
            new_index.setdefault(ro.namespace, _NamespaceIndex()).overrides[ro.name] = ro
            new_flat[(ro.namespace, ro.name)] = ro

        is_reconnect = self._ready.is_set()
        with self._lock:
            old_flat = {
                (ns, name): ro
                for ns, idx in self._index.items()
                for name, ro in idx.overrides.items()
            }
            self._index = new_index
        self._ready.set()

        cr_count = sum(len(idx.overrides) for idx in new_index.values())
        if is_reconnect:
            added = synth_modified = deleted = 0
            for key, new_ro in new_flat.items():
                old_ro = old_flat.get(key)
                if old_ro is None:
                    self._fire_callback("ADDED", new_ro, None)
                    added += 1
                elif old_ro != new_ro:
                    self._fire_callback("MODIFIED", new_ro, old_ro)
                    synth_modified += 1
            for key, old_ro in old_flat.items():
                if key not in new_flat:
                    self._fire_callback("DELETED", old_ro, old_ro)
                    deleted += 1
            _log.info(
                "resource-override cache re-populated: %d CRs across %d namespaces"
                " (synthetic events: +%d ~%d -%d)",
                cr_count, len(new_index), added, synth_modified, deleted,
            )
        else:
            _log.info("resource-override cache populated: %d CRs across %d namespaces",
                      cr_count, len(new_index))

    def _watch_loop(self) -> None:
        w = watch.Watch()
        try:
            for event in w.stream(
                self._api.list_cluster_custom_object,
                group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL,
                resource_version=self._resource_version,
                timeout_seconds=300,
            ):
                if self._stop.is_set():
                    w.stop()
                    return
                self._apply_event(event)
        finally:
            w.stop()

    def _apply_event(self, event: dict) -> None:
        kind = event.get("type")
        obj = event.get("object") or {}
        ro = _parse(obj)
        if ro is None:
            # Effective-delete fast path: a CR whose new spec we can't
            # parse (operator cleared matchLabels, emptied containers,
            # …). If we previously cached a valid version under the
            # same (namespace, name), drop it so admissions stop
            # matching the stale state. Without this the stale entry
            # survives until the next _initial_list self-heal — up to
            # the 300s watch timeout, during which pods still get
            # mutated by a CR the operator already retracted.
            metadata = obj.get("metadata") or {}
            namespace = metadata.get("namespace")
            name = metadata.get("name")
            if not namespace or not name or kind not in ("MODIFIED", "DELETED"):
                return
            prev: ResourceOverride | None = None
            with self._lock:
                ns_idx = self._index.get(namespace)
                if ns_idx is None or name not in ns_idx.overrides:
                    return
                prev = ns_idx.overrides.pop(name)
                if not ns_idx.overrides:
                    self._index.pop(namespace, None)
            if prev is not None:
                # Fire synthetic DELETED so rollout-trigger / status
                # updater learn the CR is effectively gone.
                self._fire_callback("DELETED", prev, prev)
            return
        rv = obj.get("metadata", {}).get("resourceVersion")
        if rv:
            self._resource_version = rv

        prev: ResourceOverride | None = None
        with self._lock:
            ns_idx = self._index.setdefault(ro.namespace, _NamespaceIndex())
            prev = ns_idx.overrides.get(ro.name)
            if kind in ("ADDED", "MODIFIED"):
                ns_idx.overrides[ro.name] = ro
            elif kind == "DELETED":
                ns_idx.overrides.pop(ro.name, None)
                if not ns_idx.overrides:
                    self._index.pop(ro.namespace, None)

        # Fire the callback OUTSIDE the lock so a slow handler can never
        # stall the watch loop. Best effort: any exception is logged but
        # doesn't propagate (the watch must keep running regardless).
        if kind in ("ADDED", "MODIFIED", "DELETED"):
            self._fire_callback(kind, ro, prev)

    def _fire_callback(
        self,
        kind: str,
        ro: ResourceOverride,
        prev: ResourceOverride | None,
    ) -> None:
        if self._event_callback is None:
            return
        try:
            self._event_callback(kind, ro, prev)
        except Exception:
            _log.exception("[cr-cache] event callback raised for %s/%s",
                           ro.namespace, ro.name)


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #

def _parse(raw: dict) -> ResourceOverride | None:
    metadata = raw.get("metadata") or {}
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if not namespace or not name:
        return None

    spec = raw.get("spec") or {}
    selector = (spec.get("selector") or {}).get("matchLabels") or {}
    if not selector:
        # An empty selector would match every pod in the namespace — almost certainly
        # not what the operator meant. Treat as a no-op rather than a wildcard.
        _log.warning("resource-override %s/%s has empty matchLabels selector — ignoring", namespace, name)
        return None

    raw_containers = spec.get("containers") or []
    containers = tuple(_parse_container(c) for c in raw_containers if c.get("name"))
    if not containers:
        return None

    return ResourceOverride(
        namespace=namespace,
        name=name,
        selector_match_labels=dict(selector),
        containers=containers,
    )


def _parse_container(c: dict) -> ContainerOverride:
    return ContainerOverride(
        name=c["name"],
        requests=_string_resources(c.get("requests") or {}),
        limits=_string_resources(c.get("limits") or {}),
    )


def _string_resources(d: Iterable[tuple[str, object]] | dict) -> dict[str, str]:
    """Normalise resource quantities to plain strings ('100m', '256Mi').

    Kubernetes accepts ints and quantity-like strings interchangeably; admission patches
    must be strings to match how the kubelet validates pod specs.
    """
    if isinstance(d, dict):
        items = d.items()
    else:
        items = d
    return {str(k): str(v) for k, v in items}


# --------------------------------------------------------------------------- #
# Namespace cache (opt-in filter)                                              #
# --------------------------------------------------------------------------- #

class NamespaceCache:
    """Thread-safe in-memory cache of Namespaces opted in to the tool.

    The webhook consults this cache as the *first* step of every admission to
    skip pods in namespaces that didn't opt in. Without it, every pod CREATE in
    every non-system namespace cluster-wide would walk the ResourceOverride cache
    and do label matching only to find no match — wasted work for the majority
    of admissions in any real cluster.

    Public surface:
      start()                   — begin the background watch goroutine; non-blocking.
      stop()                    — signal the watch goroutine to exit; idempotent.
      ready()                   — True once the initial list has populated the cache.
      is_enabled(namespace)     — True iff the namespace carries the opt-in annotation.
      annotations(namespace)    — Annotations dict for the namespace (empty when absent).

    The cache stores annotations for **all** namespaces, not just opted-in ones.
    A typical cluster has tens of namespaces; the memory cost is negligible and
    keeping all of them simplifies the watch loop (no edge cases around an
    annotation flip removing a namespace from the cache while a pod is mid-admit).
    """

    def __init__(self, api: client.CoreV1Api):
        self._api = api
        self._namespaces: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._resource_version: str | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="namespace-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def ready(self) -> bool:
        return self._ready.is_set()

    # ------------------------------------------------------------------ #
    # Read path (admission hot path)                                       #
    # ------------------------------------------------------------------ #

    def is_enabled(self, namespace: str) -> bool:
        """True iff the namespace's annotations include the truthy opt-in marker."""
        with self._lock:
            return is_namespace_enabled(self._namespaces.get(namespace))

    def annotations(self, namespace: str) -> dict[str, str]:
        """Return a copy of the namespace's annotations, or `{}` when unknown.

        Returning a copy keeps the caller from holding a reference to the cache's
        internal dict (which the watch loop mutates in place during MODIFIED
        events). Callers can pass the result to the resolver without needing
        further locking.
        """
        with self._lock:
            return dict(self._namespaces.get(namespace, {}))

    # ------------------------------------------------------------------ #
    # Watch loop                                                           #
    # ------------------------------------------------------------------ #

    # Bootstrap retry backoff — mirrors ResourceOverrideCache. Class
    # attributes so tests can override via patch.object().
    _BOOTSTRAP_BACKOFF_INITIAL_S = 1.0
    _BOOTSTRAP_BACKOFF_MAX_S = 60.0

    def _run(self) -> None:
        # Same retry-on-bootstrap-fail pattern as ResourceOverrideCache. Pre-fix
        # a transient list_namespace failure killed this daemon thread silently;
        # the namespace cache would stay empty, `is_namespace_opted_in()`
        # returned False for every namespace, and admission proceeded as if
        # nothing opted in.
        backoff = self._BOOTSTRAP_BACKOFF_INITIAL_S
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                self._initial_list()
                break
            except Exception:
                _log.exception(
                    "namespace cache bootstrap failed (attempt %d); "
                    "retrying in %.1fs", attempt, backoff,
                )
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self._BOOTSTRAP_BACKOFF_MAX_S)
        else:
            return

        # Sibling fix mirroring ResourceOverrideCache._run().
        # Same K-failure → `_ready.clear()` + capped-exponential backoff
        # replaces the prior fixed 2.0s sleep.
        consecutive_fail = 0
        while not self._stop.is_set():
            try:
                self._watch_loop()
                consecutive_fail = 0
            except Exception:
                _log.warning("namespace watch interrupted; re-listing", exc_info=True)
                try:
                    self._initial_list()
                    consecutive_fail = 0
                except Exception:
                    consecutive_fail += 1
                    _log.exception(
                        "namespace re-list failed (consecutive=%d); "
                        "will retry with backoff",
                        consecutive_fail,
                    )
                    if consecutive_fail >= _READY_CLEAR_AFTER_K:
                        self._ready.clear()
                    if _backoff_sleep(self._stop, consecutive_fail - 1,
                                      self._BOOTSTRAP_BACKOFF_INITIAL_S,
                                      self._BOOTSTRAP_BACKOFF_MAX_S):
                        return

    def _initial_list(self) -> None:
        result = self._api.list_namespace()
        self._resource_version = (result.metadata.resource_version
                                  if result.metadata else None)
        new_index: dict[str, dict[str, str]] = {}
        for ns in result.items:
            meta = ns.metadata
            if not meta or not meta.name:
                continue
            new_index[meta.name] = dict(meta.annotations or {})
        with self._lock:
            self._namespaces = new_index
        self._ready.set()
        enabled = sum(1 for ann in new_index.values() if is_namespace_enabled(ann))
        _log.info("namespace cache populated: %d total, %d opted in",
                  len(new_index), enabled)

    def _watch_loop(self) -> None:
        w = watch.Watch()
        try:
            for event in w.stream(
                self._api.list_namespace,
                resource_version=self._resource_version,
                timeout_seconds=300,
            ):
                if self._stop.is_set():
                    w.stop()
                    return
                self._apply_event(event)
        finally:
            w.stop()

    def _apply_event(self, event: dict) -> None:
        kind = event.get("type")
        obj = event.get("object")
        if obj is None:
            return
        meta = getattr(obj, "metadata", None)
        if not meta or not meta.name:
            return
        if meta.resource_version:
            self._resource_version = meta.resource_version

        with self._lock:
            if kind in ("ADDED", "MODIFIED"):
                self._namespaces[meta.name] = dict(meta.annotations or {})
            elif kind == "DELETED":
                self._namespaces.pop(meta.name, None)
