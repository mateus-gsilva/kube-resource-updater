"""
ResourceOverride → workload auto-rollout.

When a `ResourceOverride` is created or updated and resource values
actually changed, optionally restart the workloads it patches so the
running pods pick up the new spec without waiting for an unrelated
event (release, node drain, scale).

Same trick `kubectl rollout restart` uses: stamp
`kubectl.kubernetes.io/restartedAt: <RFC3339>` on the workload's
PodTemplate annotations. Kubelet sees the template hash change and
rolls the pods (respecting `maxUnavailable` / PDB).

Hierarchy of opt-in (first layer that explicitly sets the annotation
wins; otherwise fall through):

    Workload annotation `kube-resource-updater.autoRollout: "true|false"`
        ↓
    Namespace annotation (same key)
        ↓
    Helm default (`webhook.autoRollout.enabled`, default false)

A debounce window coalesces rapid CR updates that target the same
workload — a multi-CR MR merge → 1 restart per affected workload, not N.

Skips when the spec didn't actually change. The CR cache passes the
old + new container payloads; we only fire when at least one
`requests` / `limits` field differs. Edits that only touch labels,
selectors or empty annotations are no-ops.
"""
from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass, field
from collections.abc import Iterable

from kubernetes import client
from kubernetes.client.rest import ApiException

from src import log as _log_module
from src.overrides import is_auto_rollout_enabled
from src.webhook_cache import (
    ContainerOverride,
    NamespaceCache,
    ResourceOverride,
)

_log = _log_module.get(__name__)


@dataclass
class _PendingRestart:
    """One workload waiting for its debounced restart timer to fire.

    `attempt` tracks how many PATCH attempts have already failed for this
    workload. Bumped each time `_restart_workload` re-inserts after a
    transient ApiException; reset to 0 on success. Capped at
    `_PATCH_MAX_ATTEMPTS` — beyond that the entry is dropped and the
    rollout waits for the next CR event.
    """
    fire_at: datetime.datetime
    cr_names: set[str] = field(default_factory=set)
    attempt: int = 0


# Bounded retry on transient PATCH failure. Three retries with
# exponential backoff (30s, 60s, 120s) covers most transient PDB blocks
# and apiserver flakes without spinning forever on a permanent failure.
_PATCH_MAX_ATTEMPTS = 3
_PATCH_BACKOFF_BASE_S = 30


class RolloutTrigger:
    """Watches CR ADD/MODIFY events and restarts the matching workloads.

    Public surface:
      handle_event(event_type, ro, old_ro)  — invoked from the CR cache.
                                               event_type in {"ADDED",
                                               "MODIFIED"}; old_ro is
                                               None on ADD.
      start()                                — spawn the debounce timer
                                               daemon. Idempotent.
      stop()                                 — signal the daemon to exit.
    """

    def __init__(
        self,
        apps_api: client.AppsV1Api,
        ns_cache: NamespaceCache,
        helm_default_enabled: bool,
        debounce_seconds: int = 30,
    ):
        self._apps = apps_api
        self._ns_cache = ns_cache
        self._helm_default = helm_default_enabled
        self._debounce = max(1, debounce_seconds)
        self._pending: dict[tuple[str, str, str], _PendingRestart] = {}
        # Key is (namespace, target_kind, target_name) — kind ("Deployment"
        # / "StatefulSet" / "DaemonSet") matters because workloads of
        # different kinds can share a name in the same namespace.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._tick_loop, name="webhook-rollout-trigger", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._debounce + 5)

    # ------------------------------------------------------------------ #
    # CR event entrypoint                                                  #
    # ------------------------------------------------------------------ #

    def handle_event(
        self,
        event_type: str,
        ro: ResourceOverride,
        old_ro: ResourceOverride | None,
    ) -> None:
        """Decide whether to schedule a workload restart for this CR event.

        Skips silently when:
          - event isn't ADDED/MODIFIED (DELETED cancels pending entries);
          - resource values didn't actually change vs the previous
            cached version (same container set + same requests/limits);
          - selector matches no workload in the namespace;
          - the resolved hierarchy says auto-rollout is off for the
            target workload.

        On DELETED: removes every pending entry whose cr_names set contains
        ro.name in ro.namespace.  A workload entry whose last contributing CR
        was deleted has its restart cancelled to prevent a spurious rolling
        restart after the operator removes the ResourceOverride.

        Logs a warning and skips on apiserver errors during the workload
        lookup — the rollout is best-effort, the next event will retry.
        """
        if event_type == "DELETED":
            self._cancel_pending_for_cr(ro.namespace, ro.name)
            return

        if event_type not in ("ADDED", "MODIFIED"):
            return

        # No-op edit guard: ADDed CRs are always candidates (no `old_ro`
        # baseline). MODIFIED CRs only count if at least one container's
        # requests or limits changed.
        if event_type == "MODIFIED" and old_ro is not None and not _resources_changed(old_ro, ro):
            _log.debug(
                "[rollout] %s/%s: spec unchanged at the resource level, skip",
                ro.namespace, ro.name,
            )
            return

        try:
            workloads = self._workloads_matching(ro)
        except ApiException as exc:
            _log.warning(
                "[rollout] failed to list workloads in %s: %s — skipping rollout for %s",
                ro.namespace, exc, ro.name,
            )
            return

        if not workloads:
            _log.debug(
                "[rollout] %s/%s: selector matched no Deployment / StatefulSet / DaemonSet",
                ro.namespace, ro.name,
            )
            return

        ns_annotations = self._ns_cache.annotations(ro.namespace)
        for kind, name, workload_annotations in workloads:
            if not is_auto_rollout_enabled(self._helm_default, ns_annotations, workload_annotations):
                _log.debug(
                    "[rollout] %s/%s/%s: auto-rollout disabled by hierarchy, skip",
                    ro.namespace, kind, name,
                )
                continue
            self._schedule(ro.namespace, kind, name, ro.name)

    # ------------------------------------------------------------------ #
    # Internal: workload lookup                                            #
    # ------------------------------------------------------------------ #

    def _workloads_matching(
        self, ro: ResourceOverride,
    ) -> list[tuple[str, str, dict[str, str]]]:
        """Return (kind, name, annotations) tuples for every Deployment,
        StatefulSet, or DaemonSet in the CR's namespace whose pod template
        labels include every key/value of `ro.selector_match_labels`.

        We list rather than label-select on the apiserver because the CR's
        selector matches POD labels (set on the PodTemplate), and the
        cluster-side label selector on `Deployment.spec.template.metadata.labels`
        doesn't work as a server-side filter. Listing the whole namespace is
        cheap (≤ a few dozen workloads per namespace in practice).
        """
        deps = self._apps.list_namespaced_deployment(ro.namespace).items
        sts = self._apps.list_namespaced_stateful_set(ro.namespace).items
        dss = self._apps.list_namespaced_daemon_set(ro.namespace).items

        out: list[tuple[str, str, dict[str, str]]] = []
        for kind, items in (("Deployment", deps), ("StatefulSet", sts), ("DaemonSet", dss)):
            for item in items:
                tmpl = (item.spec.template if item.spec else None)
                tmpl_meta = (tmpl.metadata if tmpl else None)
                tmpl_labels = (tmpl_meta.labels if tmpl_meta else None) or {}
                if _labels_match(ro.selector_match_labels, tmpl_labels):
                    annotations = (item.metadata.annotations if item.metadata else None) or {}
                    out.append((kind, item.metadata.name, dict(annotations)))
        return out

    # ------------------------------------------------------------------ #
    # Internal: debounce + apply                                           #
    # ------------------------------------------------------------------ #

    def _schedule(self, namespace: str, kind: str, name: str, cr_name: str) -> None:
        """Bump the debounce timer for (namespace, kind, name).

        Multiple events landing inside a single window collapse into one
        restart fired `debounce_seconds` after the LAST event — the
        common multi-CR-merge case results in one rolling restart per
        affected workload.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        fire_at = now + datetime.timedelta(seconds=self._debounce)
        key = (namespace, kind, name)
        with self._lock:
            entry = self._pending.get(key)
            if entry is None:
                entry = _PendingRestart(fire_at=fire_at)
                self._pending[key] = entry
            else:
                entry.fire_at = fire_at  # extend the window
            entry.cr_names.add(cr_name)
        _log.info(
            "[rollout] scheduled restart of %s/%s %s in %ds (trigger CR: %s/%s)",
            namespace, kind, name, self._debounce, namespace, cr_name,
        )

    def _cancel_pending_for_cr(self, namespace: str, cr_name: str) -> None:
        """Remove pending restarts that were triggered solely by the deleted CR.

        Called on DELETED events.  Iterates over self._pending and removes
        cr_name from every entry's cr_names set that belongs to the same
        namespace.  An entry whose cr_names set becomes empty after removal
        is dropped entirely — there are no remaining CRs that contributed to
        this restart, so firing it would be a spurious rollout.

        Entries that still have other contributing CR names are left in place;
        their timers continue normally.
        """
        with self._lock:
            to_drop = []
            for key, entry in self._pending.items():
                key_ns = key[0]
                if key_ns != namespace:
                    continue
                entry.cr_names.discard(cr_name)
                if not entry.cr_names:
                    to_drop.append(key)
                    _log.info(
                        "[rollout] cancelled pending restart of %s/%s %s "
                        "— triggering CR %s/%s was deleted",
                        key_ns, key[1], key[2], namespace, cr_name,
                    )
            for key in to_drop:
                del self._pending[key]

    def _tick_loop(self) -> None:
        # Sleep in 1s slices so stop() returns quickly. fire_at granularity
        # is ~1s; no need to spin.
        while not self._stop.wait(1.0):
            self._fire_due()
        # Drain remaining timers on shutdown.
        self._fire_due(force=True)

    def _fire_due(self, *, force: bool = False) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        with self._lock:
            ready: list[tuple[tuple[str, str, str], _PendingRestart]] = []
            keep: dict[tuple[str, str, str], _PendingRestart] = {}
            for key, entry in self._pending.items():
                if force or entry.fire_at <= now:
                    ready.append((key, entry))
                else:
                    keep[key] = entry
            self._pending = keep
        for key, entry in ready:
            self._restart_workload(key, entry)

    def _restart_workload(
        self, key: tuple[str, str, str], entry: _PendingRestart,
    ) -> None:
        """Patch the workload's PodTemplate annotations with restartedAt.

        Mirrors what `kubectl rollout restart` does. Kubelet diffs the
        template, sees the annotation map changed, treats it as a new
        revision and rolls. PDB / maxUnavailable are honoured by the
        Deployment / StatefulSet controllers — we don't need any extra
        guards here.

        Retry policy on ApiException:
          - 404 (workload deleted between schedule and fire): drop entry.
          - any other status: re-insert with exponential backoff up to
            `_PATCH_MAX_ATTEMPTS` retries, then give up. Without this,
            pre-fix a single PDB-blocked PATCH silently dropped the
            rollout — pods kept stale resources until the operator made
            another CR change.
        """
        namespace, kind, name = key
        cr_names = entry.cr_names
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt":
                                datetime.datetime.now(datetime.timezone.utc)
                                    .isoformat().replace("+00:00", "Z"),
                        },
                    },
                },
            },
        }
        try:
            if kind == "Deployment":
                self._apps.patch_namespaced_deployment(name, namespace, body)
            elif kind == "StatefulSet":
                self._apps.patch_namespaced_stateful_set(name, namespace, body)
            elif kind == "DaemonSet":
                self._apps.patch_namespaced_daemon_set(name, namespace, body)
            else:
                _log.warning("[rollout] unsupported workload kind %s for %s/%s",
                             kind, namespace, name)
                return
        except ApiException as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                # Workload deleted between schedule and PATCH. Drop the
                # entry — there is nothing to roll.
                _log.info(
                    "[rollout] %s %s/%s not found at PATCH time — dropping pending restart",
                    kind, namespace, name,
                )
                return
            entry.attempt += 1
            if entry.attempt >= _PATCH_MAX_ATTEMPTS:
                _log.warning(
                    "[rollout] PATCH %s %s/%s gave up after %d attempts (last: %s) — "
                    "pod restart not applied; next CR event will re-schedule",
                    kind, namespace, name, entry.attempt, exc,
                )
                return
            backoff = _PATCH_BACKOFF_BASE_S * (2 ** (entry.attempt - 1))
            next_fire = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=backoff)
            _log.warning(
                "[rollout] PATCH %s %s/%s failed (attempt %d/%d, status=%s): %s — "
                "retrying in %ds",
                kind, namespace, name, entry.attempt, _PATCH_MAX_ATTEMPTS,
                status, exc, backoff,
            )
            entry.fire_at = next_fire
            with self._lock:
                # Merge with whatever may have landed for the same key
                # during the failed PATCH. Take the LATER fire_at so a
                # fresh CR event extending the debounce window still wins.
                existing = self._pending.get(key)
                if existing is not None:
                    existing.cr_names |= entry.cr_names
                    existing.fire_at = max(existing.fire_at, entry.fire_at)
                    existing.attempt = max(existing.attempt, entry.attempt)
                else:
                    self._pending[key] = entry
            return
        # Success — clear attempt counter (defensive; entry was already
        # removed from _pending in _fire_due, but if a concurrent re-add
        # raced in we want the new entry to start fresh).
        entry.attempt = 0
        _log.info(
            "[rollout] restarted %s %s/%s (CRs: %s)",
            kind, namespace, name, ", ".join(sorted(cr_names)),
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """Standard matchLabels semantics: every (k, v) in the selector must
    appear unchanged in the labels dict. Empty selector returns False so
    a malformed CR doesn't match every workload in the cluster — same
    rule the cache enforces (see _parse() in src/webhook_cache.py).
    """
    if not selector:
        return False
    for k, v in selector.items():
        if labels.get(k) != v:
            return False
    return True


def _resources_changed(old: ResourceOverride, new: ResourceOverride) -> bool:
    """True if any container's `requests` or `limits` differ between old and new.

    Normalises by container name so reordering is a no-op. Containers
    present in only one side counts as changed (an added / removed
    container materially affects the resulting pod spec).
    """
    def _index(containers: Iterable[ContainerOverride]) -> dict[str, ContainerOverride]:
        return {c.name: c for c in containers}

    a, b = _index(old.containers), _index(new.containers)
    if set(a) != set(b):
        return True
    for name in a:
        if a[name].requests != b[name].requests or a[name].limits != b[name].limits:
            return True
    return False
