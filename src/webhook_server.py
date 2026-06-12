"""
Mutation webhook HTTP server.

Wires together:
  - ResourceOverrideCache (src/webhook_cache.py) — informer-style watch + in-memory index.
  - build_patches (src/webhook_patch.py)         — compute container resource patches.
  - aiohttp                                       — TLS server bound to certs from cert-manager.

The server exposes:
  POST /mutate-pod      — admission webhook endpoint (TLS, AdmissionReview v1)
  GET  /healthz         — liveness (always 200 once the cache is started)
  GET  /readyz          — readiness (200 only after initial cache populated)
  GET  /metrics         — Prometheus exposition (admission counters)

The server intentionally has no business knowledge beyond 'apply patches from cache'.
All decisions about which workloads to manage live in the CR (selector + container list).
"""
from __future__ import annotations

import base64
import json
import os
import ssl
from typing import Any

from aiohttp import web

from kubernetes import client

from src import log as _log_module
from src.overrides import is_workload_skipped
from src.webhook_cache import NamespaceCache, ResourceOverrideCache
from src.webhook_patch import build_patches, patches_to_jsonpatch, patched_containers_per_cr
from src.webhook_rollout import RolloutTrigger
from src.webhook_status import StatusUpdater
from src import webhook_validate

_log = _log_module.get(__name__)


# --------------------------------------------------------------------------- #
# Metrics (lightweight; no prometheus_client dep — simple text exposition)    #
# --------------------------------------------------------------------------- #

class _Metrics:
    """Counters exposed via /metrics in Prometheus text exposition format.

    Kept dependency-free on purpose: the tool already ships with `requests` and
    `kubernetes`, both heavy enough; adding `prometheus_client` for a handful of
    counters is overkill. The format is simple and stable.
    """
    def __init__(self) -> None:
        self.admissions_total = 0
        self.admissions_patched = 0
        self.admissions_skipped_cache_unready = 0
        self.admissions_skipped_namespace_not_enabled = 0
        self.admissions_skipped_pod_skip_annotation = 0
        self.admissions_skipped_no_match = 0
        self.admission_errors = 0
        # Validating webhook (separate path; only emitted when
        # webhook.validating.enabled is on and the chart wires the
        # ValidatingWebhookConfiguration).
        self.validations_total = 0
        self.validations_rejected = 0

    def render(self) -> str:
        return (
            "# HELP resource_updater_webhook_admissions_total Pod admission requests received.\n"
            "# TYPE resource_updater_webhook_admissions_total counter\n"
            f"resource_updater_webhook_admissions_total {self.admissions_total}\n"
            "# HELP resource_updater_webhook_admissions_patched_total Admissions where at least one container was patched.\n"
            "# TYPE resource_updater_webhook_admissions_patched_total counter\n"
            f"resource_updater_webhook_admissions_patched_total {self.admissions_patched}\n"
            "# HELP resource_updater_webhook_admissions_skipped_cache_unready_total Admissions skipped because a cache had not finished initial list.\n"
            "# TYPE resource_updater_webhook_admissions_skipped_cache_unready_total counter\n"
            f"resource_updater_webhook_admissions_skipped_cache_unready_total {self.admissions_skipped_cache_unready}\n"
            "# HELP resource_updater_webhook_admissions_skipped_namespace_not_enabled_total Admissions short-circuited because the namespace lacks the kube-resource-updater.enabled annotation.\n"
            "# TYPE resource_updater_webhook_admissions_skipped_namespace_not_enabled_total counter\n"
            f"resource_updater_webhook_admissions_skipped_namespace_not_enabled_total {self.admissions_skipped_namespace_not_enabled}\n"
            "# HELP resource_updater_webhook_admissions_skipped_pod_skip_annotation_total Admissions short-circuited because the pod carries kube-resource-updater.skip=true.\n"
            "# TYPE resource_updater_webhook_admissions_skipped_pod_skip_annotation_total counter\n"
            f"resource_updater_webhook_admissions_skipped_pod_skip_annotation_total {self.admissions_skipped_pod_skip_annotation}\n"
            "# HELP resource_updater_webhook_admissions_skipped_no_match_total Admissions skipped because no override matched the pod.\n"
            "# TYPE resource_updater_webhook_admissions_skipped_no_match_total counter\n"
            f"resource_updater_webhook_admissions_skipped_no_match_total {self.admissions_skipped_no_match}\n"
            "# HELP resource_updater_webhook_admission_errors_total Admissions returned with an error response (failurePolicy: Ignore makes these non-blocking).\n"
            "# TYPE resource_updater_webhook_admission_errors_total counter\n"
            f"resource_updater_webhook_admission_errors_total {self.admission_errors}\n"
            "# HELP resource_updater_webhook_validations_total ResourceOverride validations seen by the validating webhook.\n"
            "# TYPE resource_updater_webhook_validations_total counter\n"
            f"resource_updater_webhook_validations_total {self.validations_total}\n"
            "# HELP resource_updater_webhook_validations_rejected_total ResourceOverride validations that returned Allowed=false (selector/container overlap).\n"
            "# TYPE resource_updater_webhook_validations_rejected_total counter\n"
            f"resource_updater_webhook_validations_rejected_total {self.validations_rejected}\n"
        )


# --------------------------------------------------------------------------- #
# AdmissionReview helpers                                                      #
# --------------------------------------------------------------------------- #

def _allow_response(uid: str, patches: list[dict] | None = None,
                    warnings: list[str] | None = None) -> dict[str, Any]:
    """Build an AdmissionReview response with optional patch and warnings.

    The response always allows the request (failurePolicy: Ignore is paired with
    'always allow' here — we patch when we can, skip silently otherwise).
    """
    response: dict[str, Any] = {
        "uid": uid,
        "allowed": True,
    }
    if patches:
        response["patchType"] = "JSONPatch"
        response["patch"] = base64.b64encode(json.dumps(patches).encode()).decode()
    if warnings:
        response["warnings"] = warnings
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": response,
    }


def _error_response(uid: str, message: str) -> dict[str, Any]:
    """Build an admission response that allows the request but surfaces an error.

    With failurePolicy: Ignore on the MutatingWebhookConfiguration, returning
    `allowed: true` even on error means a webhook bug never blocks deployments.
    The error is recorded in the metrics counter and the warnings field.
    """
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": True,
            "warnings": [f"resource-updater-webhook: {message}"],
        },
    }


# --------------------------------------------------------------------------- #
# Handlers                                                                     #
# --------------------------------------------------------------------------- #

def _make_mutate_handler(
    cr_cache: ResourceOverrideCache,
    ns_cache: NamespaceCache,
    metrics: _Metrics,
    status_updater: StatusUpdater | None,
):
    async def mutate(request: web.Request) -> web.Response:
        metrics.admissions_total += 1
        try:
            review = await request.json()
        except Exception:
            metrics.admission_errors += 1
            return web.json_response(
                _error_response("", "invalid AdmissionReview JSON"),
                status=200,
            )

        req = review.get("request") or {}
        uid = req.get("uid", "")
        pod = req.get("object") or {}
        pod_meta = pod.get("metadata") or {}
        namespace = pod_meta.get("namespace") or req.get("namespace") or ""

        # Cache-readiness gate covers both informers — without either, we cannot
        # safely decide. failurePolicy: Ignore on the MutatingWebhookConfiguration
        # turns this into a benign "admit with chart defaults" path.
        if not (cr_cache.ready() and ns_cache.ready()):
            metrics.admissions_skipped_cache_unready += 1
            return web.json_response(_allow_response(uid))

        # Phase-4 short-circuit #1: namespace did not opt in. Cheapest path —
        # no CR cache scan, no label match. Most pod admissions in a real
        # cluster land here (a few opted-in namespaces vs many that aren't).
        if not ns_cache.is_enabled(namespace):
            metrics.admissions_skipped_namespace_not_enabled += 1
            return web.json_response(_allow_response(uid))

        # Phase-4 short-circuit #2: per-pod escape hatch. Lets an operator
        # bypass the webhook on a single deployment without touching the
        # namespace's opt-in state. annotations live next to labels in the
        # AdmissionReview — read directly, no cache needed.
        if is_workload_skipped(pod_meta.get("annotations") or {}):
            metrics.admissions_skipped_pod_skip_annotation += 1
            return web.json_response(_allow_response(uid))

        labels = pod_meta.get("labels") or {}

        try:
            matches = cr_cache.lookup(namespace, labels)
        except Exception as exc:
            _log.exception("cr cache lookup failed for pod in %s", namespace)
            metrics.admission_errors += 1
            return web.json_response(_error_response(uid, f"cache lookup failed: {exc}"))

        if not matches:
            metrics.admissions_skipped_no_match += 1
            return web.json_response(_allow_response(uid))

        try:
            ops = build_patches(pod, matches)
        except Exception as exc:
            _log.exception("patch build failed for pod in %s", namespace)
            metrics.admission_errors += 1
            return web.json_response(_error_response(uid, f"patch build failed: {exc}"))

        if not ops:
            metrics.admissions_skipped_no_match += 1
            return web.json_response(_allow_response(uid))

        # Status writer is optional (chart's webhook.status.enabled). When
        # off, status_updater is None and we skip the bookkeeping entirely.
        # Record `lastAppliedAt` ONLY for CRs that actually contributed
        # a container patch. Pre-fix the loop ran over every selector-
        # matched CR, including ones whose container list didn't overlap
        # with the pod and ones that lost the last-wins on duplicate
        # container names — `kubectl get ro -A` then showed a fresh
        # timestamp on CRs that contributed nothing for THIS pod.
        # (Audit framework bug-hunt on webhook_server.py admission path.)
        #
        # Skip the status write entirely on dry-run admissions.
        # MutatingWebhookConfiguration declares sideEffects: None, which is
        # the k8s contract that the webhook produces NO persistent side
        # effects on ANY request — including `kubectl apply --dry-run=server`.
        # The AdmissionReview request carries request.dryRun=true in that
        # case.  Writing to ResourceOverride.status from a dry-run path
        # violates that contract.  The mutation patches themselves are still
        # computed and returned; the apiserver discards them on dry-run.
        # sideEffects: None remains correct after this fix — the code is now
        # genuinely side-effect-free on every code path, including dry-run.
        if status_updater is not None and not req.get("dryRun"):
            containers_per_cr = patched_containers_per_cr(pod, matches)
            for cr_id, container_set in containers_per_cr.items():
                ns_part, name_part = cr_id.split("/", 1)
                status_updater.record(ns_part, name_part, container_set)

        metrics.admissions_patched += 1
        return web.json_response(_allow_response(uid, patches_to_jsonpatch(ops)))

    return mutate


def _make_validate_handler(cr_cache: ResourceOverrideCache, metrics: _Metrics):
    """AdmissionReview handler for ResourceOverride CREATE/UPDATE.

    Delegates the overlap check to `src/webhook_validate.py`; metrics
    counters mirror the mutating side so an operator can dashboard
    "rejections" the same way they do "patches".
    """
    async def validate(request: web.Request) -> web.Response:
        metrics.validations_total += 1
        try:
            review = await request.json()
        except Exception:
            metrics.admission_errors += 1
            return web.json_response(
                webhook_validate.allow("", "invalid AdmissionReview JSON"),
                status=200,
            )
        try:
            response = webhook_validate.validate(review, cr_cache)
        except Exception:
            _log.exception("[validate] handler raised; admitting to avoid lockout")
            metrics.admission_errors += 1
            uid = (review.get("request") or {}).get("uid", "")
            return web.json_response(webhook_validate.allow(uid, "validator error"))
        if not response.get("response", {}).get("allowed", True):
            metrics.validations_rejected += 1
        return web.json_response(response)
    return validate


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _make_readyz_handler(cr_cache: ResourceOverrideCache, ns_cache: NamespaceCache):
    async def readyz(_request: web.Request) -> web.Response:
        # Both informers must have completed their initial list before the
        # webhook can safely admit traffic. Returning 503 keeps the apiserver
        # from sending requests during the few hundred milliseconds of bootstrap.
        if cr_cache.ready() and ns_cache.ready():
            return web.Response(text="ok")
        return web.Response(status=503, text="cache not ready")
    return readyz


def _make_metrics_handler(metrics: _Metrics):
    async def serve(_request: web.Request) -> web.Response:
        # Parameterised media type (Prometheus text exposition) goes in an
        # explicit header — aiohttp's content_type= slot takes a bare token.
        return web.Response(
            body=metrics.render().encode("utf-8"),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )
    return serve


# --------------------------------------------------------------------------- #
# Entrypoint                                                                   #
# --------------------------------------------------------------------------- #

def run(
    port: int,
    cert_dir: str,
    metrics_port: int,
    custom_objects_api,
    *,
    status_enabled: bool = True,
    status_flush_interval_seconds: int = 30,
    auto_rollout_enabled: bool = False,
    auto_rollout_debounce_seconds: int = 30,
) -> None:
    """Start the webhook server. Blocks until SIGTERM/SIGINT.

    `status_enabled` gates the in-memory writer that PATCHes
    `ResourceOverride.status.appliedToPodCount` + `lastAppliedAt` on a
    `status_flush_interval_seconds` ticker. The chart toggles this via
    `webhook.status.enabled`; when off, the chart also drops the
    `resourceoverrides/status:patch` RBAC verb so the running webhook
    never has the permission to escalate beyond what its config allows.
    """
    cert_file = os.path.join(cert_dir, "tls.crt")
    key_file = os.path.join(cert_dir, "tls.key")
    if not (os.path.exists(cert_file) and os.path.exists(key_file)):
        raise FileNotFoundError(
            f"webhook serving cert not found at {cert_dir} (expected tls.crt and tls.key — "
            "is cert-manager mounting the secret?)"
        )

    ns_cache = NamespaceCache(client.CoreV1Api())
    ns_cache.start()

    # Auto-rollout trigger is wired into the CR cache via the event callback,
    # so we must build it BEFORE starting the cache. The cache fires
    # ADDED/MODIFIED/DELETED events for every override; the trigger filters
    # on opt-in hierarchy (workload > namespace > helm-default) and patches
    # workload PodTemplates with `kubectl.kubernetes.io/restartedAt` so
    # kubelet rolls them.
    rollout_trigger: RolloutTrigger | None = None
    cr_event_callback = None
    if auto_rollout_enabled:
        # Helm-default in the hierarchy is FALSE intentionally: turning the
        # feature on cluster-wide enables the *machinery* (the trigger
        # process + apps/* RBAC), but the actual decision is opt-in per
        # workload / namespace via the `kube-resource-updater.autoRollout`
        # annotation. Otherwise the chart toggle would surprise an
        # operator with an immediate rolling-restart of every opted-in
        # workload as soon as the next sync changes anything.
        rollout_trigger = RolloutTrigger(
            apps_api=client.AppsV1Api(),
            ns_cache=ns_cache,
            helm_default_enabled=False,
            debounce_seconds=auto_rollout_debounce_seconds,
        )
        rollout_trigger.start()
        cr_event_callback = rollout_trigger.handle_event
        _log.info("[rollout] auto-rollout enabled (debounce %ds, opt-in per workload/namespace via annotation)",
                  auto_rollout_debounce_seconds)
    else:
        _log.info("[rollout] auto-rollout disabled (webhook.autoRollout.enabled=false)")

    cr_cache = ResourceOverrideCache(custom_objects_api, event_callback=cr_event_callback)
    cr_cache.start()

    status_updater: StatusUpdater | None = None
    if status_enabled:
        status_updater = StatusUpdater(custom_objects_api, status_flush_interval_seconds)
        status_updater.start()
        _log.info("[status] writer enabled (flush every %ds)", status_flush_interval_seconds)
    else:
        _log.info("[status] writer disabled (webhook.status.enabled=false)")

    metrics = _Metrics()

    # Two separate aiohttp apps: HTTPS for admission (mutual contract with kube-apiserver),
    # plain HTTP for /healthz, /readyz, /metrics on a separate port. The metrics endpoint
    # must NOT live on the TLS port — Prometheus scraping expects HTTP and we don't want to
    # ship CA bundles around just to scrape counters.
    admission_app = web.Application()
    admission_app.router.add_post(
        "/mutate-pod",
        _make_mutate_handler(cr_cache, ns_cache, metrics, status_updater),
    )
    # Validating endpoint shares the TLS server with the mutating one —
    # same cert, same Service, different path. The chart's
    # ValidatingWebhookConfiguration (gated on webhook.validating.enabled)
    # is what decides whether the apiserver actually invokes it. With
    # the chart toggle off, the route exists but receives no traffic.
    admission_app.router.add_post(
        "/validate-resourceoverride",
        _make_validate_handler(cr_cache, metrics),
    )

    ops_app = web.Application()
    ops_app.router.add_get("/healthz", _healthz)
    ops_app.router.add_get("/readyz", _make_readyz_handler(cr_cache, ns_cache))
    ops_app.router.add_get("/metrics", _make_metrics_handler(metrics))

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(cert_file, key_file)

    _log.info("webhook listening on :%d (TLS) and ops on :%d", port, metrics_port)

    # Run admission TLS and ops plain-HTTP servers on the same event loop.
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    admission_runner = web.AppRunner(admission_app)
    ops_runner = web.AppRunner(ops_app)

    async def _serve():
        await admission_runner.setup()
        await ops_runner.setup()
        admission_site = web.TCPSite(admission_runner, host="0.0.0.0", port=port, ssl_context=ssl_ctx)
        ops_site = web.TCPSite(ops_runner, host="0.0.0.0", port=metrics_port)
        await admission_site.start()
        await ops_site.start()

    loop.run_until_complete(_serve())

    # Audit v2 finding D14: kubelet sends SIGTERM (not SIGINT) when
    # terminating the pod. Pre-1.21.0 the loop only caught
    # KeyboardInterrupt (SIGINT, the ctrl-C signal), so the cleanup
    # block never ran on a k8s shutdown — daemons leaked, in-flight
    # admission requests cut abruptly, cache watches left dangling
    # for apiserver to time out. Register a SIGTERM handler that
    # stops the event loop the same way KeyboardInterrupt would, so
    # the finally-block cleanup runs uniformly on both shutdown paths.
    import signal as _signal
    def _request_shutdown() -> None:
        _log.info("webhook shutting down (SIGTERM)")
        loop.stop()
    try:
        loop.add_signal_handler(_signal.SIGTERM, _request_shutdown)
        loop.add_signal_handler(_signal.SIGINT, _request_shutdown)
    except (NotImplementedError, RuntimeError):
        # Windows / some restricted contexts don't allow add_signal_handler;
        # fall back to the KeyboardInterrupt path on those.
        pass

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        # Belt + suspenders. add_signal_handler is supposed to convert
        # SIGINT into our handler, but tests that run loop.run_forever
        # in foreground without signal redirection may still raise here.
        _log.info("webhook shutting down (KeyboardInterrupt)")
    finally:
        loop.run_until_complete(admission_runner.cleanup())
        loop.run_until_complete(ops_runner.cleanup())
        cr_cache.stop()
        ns_cache.stop()
        if status_updater is not None:
            status_updater.stop()
        if rollout_trigger is not None:
            rollout_trigger.stop()
        loop.close()
