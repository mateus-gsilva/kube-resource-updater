"""
ResourceOverride status writer for the admission webhook.

Stamps `status.lastAppliedAt` and an `Applied` `status.conditions` entry on
every CR the webhook actually uses to patch a pod, so `kubectl get ro -A`
answers "is this CR active?" at a glance (and operators can
`kubectl wait --for=condition=Applied ro/<name>`):

    NAMESPACE   NAME        CONTAINERS   LAST-APPLIED   AGE
    backstage   backstage   backstage    12m            7d   ← active
    n8n         n8n-debug   n8n-worker   <none>         2d   ← needs investigation

`<none>` + non-trivial AGE means the CR has never patched a pod —
selector mismatch, deleted workload, label typo. Combined with `AGE`
the operator can tell "freshly created" from "long-time orphan".

The naive "PATCH on every admission" pattern hammers the apiserver in
any cluster with sustained pod churn. Pattern (Kyverno's
cert-controller, OPA Gatekeeper, cert-manager): keep an in-memory dirty
set, flush on a 30s ticker — one PATCH per CR per flush window
regardless of how many admissions landed.

Disable cluster-wide via the chart's `webhook.status.enabled: false` —
the webhook then never instantiates this writer and its RBAC verb
(`resourceoverrides/status: patch`) is dropped from the ClusterRole.
"""
from __future__ import annotations

import datetime
import threading

from kubernetes import client
from kubernetes.client.rest import ApiException

from src import log as _log_module

_log = _log_module.get(__name__)


CRD_GROUP = "kube-resource-updater.io"
CRD_VERSION = "v1"
CRD_PLURAL = "resourceoverrides"


class StatusUpdater:
    """Coalesces CR.status writes per CR id in a background daemon thread.

    Public surface:
      record(namespace, cr_name)  — called from webhook admit() per
                                    successful patch. Lock-cheap; safe
                                    to call from the aiohttp handler.
      start()                     — spawn the flush daemon. Idempotent.
      stop()                      — signal the daemon to exit; blocks
                                    briefly waiting for one final flush.
      flush_once()                — exposed for tests. Production code
                                    only calls it through the loop.
    """

    def __init__(
        self,
        custom_objects_api: client.CustomObjectsApi,
        flush_interval_seconds: int = 30,
    ):
        self._api = custom_objects_api
        self._flush_interval = max(1, flush_interval_seconds)
        # Dirty set keyed by (namespace, name). Value is a tuple of
        # (latest_admission_timestamp, frozenset_of_patched_container_names).
        # The container set is stable across identical pods of the same
        # workload (CR container list ∩ pod containers), so last-wins on
        # repeated records is correct. No pod count: that per-window counter
        # was dropped as noise; lifetime totals live in `/metrics`.
        self._dirty: dict[tuple[str, str], tuple[datetime.datetime, frozenset[str]]] = {}
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
            target=self._flush_loop, name="webhook-status-updater", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._flush_interval + 5)

    # ------------------------------------------------------------------ #
    # Hot path                                                             #
    # ------------------------------------------------------------------ #

    def record(
        self,
        namespace: str,
        cr_name: str,
        containers: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        """Record one successful admission against (namespace, cr_name).

        ``containers`` is the set of container names from this CR that
        actually contributed a patch for this pod (the CR's container list
        intersected with the pod's spec.containers, post last-wins). It is
        written to ``status.patchedContainers`` on the next flush so an
        operator can see which containers the CR manages; an empty set
        produces ``[]`` — the "this CR matches nothing" signal.

        Called from the webhook's admit() handler. Lock contention is cheap
        because aiohttp serves admissions on a single event loop thread; the
        guard is here for the flush thread that walks the same map. Last-wins
        on repeated records is correct: the container set is stable across
        identical pods of the same workload.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        with self._lock:
            self._dirty[(namespace, cr_name)] = (now, frozenset(containers))

    # ------------------------------------------------------------------ #
    # Flush                                                                #
    # ------------------------------------------------------------------ #

    def _flush_loop(self) -> None:
        # First sleep before first flush — gives the webhook a moment
        # to come up and start receiving traffic before we burn API
        # calls writing all-zero counts to every CR.
        while not self._stop.wait(self._flush_interval):
            try:
                self.flush_once()
            except Exception:
                _log.exception("[status] flush iteration failed")
        # On stop, do one final flush so in-flight counters land in
        # the API instead of being dropped on pod restart.
        try:
            self.flush_once()
        except Exception:
            _log.exception("[status] final flush on shutdown failed")

    def flush_once(self) -> int:
        """Drain the dirty map and PATCH each CR's status. Returns the
        number of CRs that received a write. Tests call this directly
        without starting the daemon thread.
        """
        with self._lock:
            snapshot, self._dirty = self._dirty, {}
        if not snapshot:
            return 0

        wrote = 0
        for (ns, name), (last_at, containers) in snapshot.items():
            ts = last_at.isoformat().replace("+00:00", "Z")
            # lastTransitionTime is set to now on every flush, not only on
            # state transitions. `Applied` only ever goes True for a CR's
            # lifetime (once it patches a pod it never un-applies), so the
            # real transition (absent → True) happens once. Re-stamping it on
            # each flush is a minor deviation from the strict k8s condition
            # convention but avoids a read-modify-write per CR per flush to
            # preserve the original value.
            body = {
                "status": {
                    "lastAppliedAt": ts,
                    "patchedContainers": sorted(containers),
                    "conditions": [
                        {
                            "type": "Applied",
                            "status": "True",
                            "reason": "PodPatched",
                            "message": (
                                "Webhook patched at least one pod from this"
                                " ResourceOverride."
                            ),
                            "lastTransitionTime": ts,
                        },
                    ],
                },
            }
            try:
                self._api.patch_namespaced_custom_object_status(
                    group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL,
                    namespace=ns, name=name, body=body,
                )
                wrote += 1
            except ApiException as exc:
                # 404 = CR was deleted between admission and flush. Not
                # worth an error log; just drop the pending entry.
                if exc.status == 404:
                    _log.debug("[status] CR %s/%s was deleted; dropping pending stamp", ns, name)
                    continue
                # Transient 4xx/5xx (rate limit, apiserver flake, network
                # blip): pre-fix the snapshot was already drained from
                # `_dirty`, so a failed PATCH silently dropped the stamp
                # forever. Re-insert via setdefault so any newer admission
                # that landed during this flush still wins (its fresher
                # timestamp preserves the monotonic property of
                # lastAppliedAt). The next flush retries. (Audit framework
                # Area 4.)
                _log.warning("[status] failed to patch %s/%s: %s — requeueing", ns, name, exc)
                with self._lock:
                    self._dirty.setdefault((ns, name), (last_at, containers))
            except Exception as exc:
                # Non-ApiException network failures (urllib3 MaxRetryError,
                # ConnectionError, etc.) must NOT escape the loop — that
                # would silently drop all remaining CRs in the snapshot.
                # Re-queue via setdefault (same semantics as the transient
                # ApiException path above).
                _log.warning("[status] unexpected error patching %s/%s: %s — requeueing", ns, name, exc)
                with self._lock:
                    self._dirty.setdefault((ns, name), (last_at, containers))
        return wrote
