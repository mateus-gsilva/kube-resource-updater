#!/usr/bin/env python3
"""
kube-resource-updater — reads resource usage from Prometheus and writes
CPU/memory requests and limits back to Git as ResourceOverride CRs.

Commands:
  check-prometheus  Test connectivity to the configured / discovered Prometheus
  sync              Discover opted-in namespaces, compute recommendations,
                      write ResourceOverride CRs to git
  webhook           Run the mutation admission webhook (long-running; reads
                      ResourceOverride CRs and patches pod resources at admission time)

Opt-in (single source of truth):
  Annotation `kube-resource-updater.enabled: "true"` on the Namespace.

Per-namespace and per-workload tunables are annotations of the same form
(see src/overrides.py for the full key list). Hierarchy: helm defaults <
namespace annotations < workload annotations.

Environment variables:
  GIT_TOKEN         Git provider token (required for sync when createMr is
                    on; GITLAB_TOKEN remains as a deprecated alias)
  CONFIG_FILE       Override the ConfigMap mount path (default:
                    /etc/kube-resource-updater/config.yaml)
"""
import argparse
import os
import sys
from pathlib import Path

from kubernetes import client

from src import log as _log_module
from src.config import Config
from src.discovery import list_enabled_namespaces
from src.k8s import load_config
from src.overrides import is_workload_skipped, resolve_for_workload, resolve_skip_containers
from src.prometheus import check_connectivity
from src.workload import WorkloadRecommendation, list_workloads
from src.writeback import log_git_credentials_state
from src.writeback_webhook import write_back_webhook_all
from src.git_provider import build_provider

_log = _log_module.get(__name__)


def _read_pod_namespace() -> "str | None":
    """Return the pod's own namespace from the projected service account, or None.

    The webhook always runs as a pod, so this file is the canonical source.
    Outside the cluster (`python main.py webhook` for local dev) the file is
    absent — caller falls back to a default.
    """
    try:
        return Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _effective_gitlab_url(gitlab_url: str, repo_url: str) -> str:
    """Return the effective GitLab base URL.

    When ``config.gitlab_url`` is set (the common case), return it directly.
    When empty, infer the scheme + host from the repo_url (same heuristic
    that write_back_webhook_all applied pre-Phase-1 refactor).
    """
    if gitlab_url:
        return gitlab_url
    from urllib.parse import urlparse
    parsed = urlparse(repo_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _resolve_prometheus_url(cfg: Config) -> str:
    """Return the explicit `config.prometheusUrl` and log it once.

    Auto-discovery (k8s service-DNS in `monitoring` + Prometheus CR
    `externalUrl` fallback) was dropped The chart's
    `templates/validate.yaml` fails `helm install` when `prometheusUrl`
    is empty, and `Config.validate` mirrors that check at runtime for
    hand-edited ConfigMap drift — so reaching this function with an
    empty URL is a hard error path. We still log nothing and return
    empty so the caller's `_log.error` can surface the configuration
    issue without duplicating the message.
    """
    if cfg.prometheus_url:
        _log.info("[prometheus] using %s", cfg.prometheus_url,
                  extra={"prometheus_url": cfg.prometheus_url})
    return cfg.prometheus_url


def _log_global_config(cfg: Config) -> None:
    """Log all active global config values once at startup."""
    rc = cfg.resource
    _log.info(
        "[config] cpu: p=%d%%  req-win=%s  lim-win=%s  margin=%d%%  "
        "floor=%s  ceil=%s  mult=%.1fx",
        round(rc.cpu_percentile * 100),
        rc.cpu_request_window, rc.cpu_limit_window,
        round(rc.effective_cpu_request_margin * 100),
        f"{rc.min_cpu_request_m}m"  if rc.min_cpu_request_m  else "off",
        f"{rc.max_cpu_request_m}m"  if rc.max_cpu_request_m  else "off",
        rc.cpu_limit_multiplier,
    )
    _log.info(
        "[config] mem: p=%d%%  req-win=%s  lim-win=%s  margin=%d%%  "
        "floor=%s  ceil=%s  mult=%.1fx",
        round(rc.mem_percentile * 100),
        rc.mem_request_window, rc.mem_limit_window,
        round(rc.effective_mem_request_margin * 100),
        f"{rc.min_memory_request_mi}Mi"  if rc.min_memory_request_mi  else "off",
        f"{rc.max_memory_request_mi}Mi"  if rc.max_memory_request_mi  else "off",
        rc.memory_limit_multiplier,
    )
    flags = []
    if cfg.grow_only:     flags.append("growOnly=true")
    if cfg.shrink_only:   flags.append("shrinkOnly=true")
    if cfg.dry_run:       flags.append("dryRun=true")
    if not cfg.create_mr: flags.append("createMr=false")
    if rc.round_values:   flags.append("roundValues=true")
    if flags:
        _log.info("[config] flags: %s", "  ".join(flags))


def _fmt_workload_list(names: list[str], limit: int = 4) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", +{len(names) - limit} more"


def _log_effective_overrides(rec: WorkloadRecommendation, eff: Config, base: Config) -> None:
    """Log per-workload values that differ from the cluster default.

    Intentionally compact — one line per workload, only fields that actually
    diverged. The `[ns]` / `[workload]` source tag is approximate (we'd have
    to re-merge to know exactly), so this groups them under `[override]`.
    """
    rc, brc = eff.resource, base.resource
    parts: list[str] = []
    for name, val, dval, fmt in [
        ("cpuPercentile",    rc.cpu_percentile,           brc.cpu_percentile,          lambda v: f"{round(v*100)}%"),
        ("memPercentile",    rc.mem_percentile,           brc.mem_percentile,          lambda v: f"{round(v*100)}%"),
        ("cpuRequestWindow", rc.cpu_request_window,       brc.cpu_request_window,      str),
        ("memRequestWindow", rc.mem_request_window,       brc.mem_request_window,      str),
        ("cpuLimitWindow",   rc.cpu_limit_window,         brc.cpu_limit_window,        str),
        ("memLimitWindow",   rc.mem_limit_window,         brc.mem_limit_window,        str),
        ("marginFraction",   rc.margin_fraction,          brc.margin_fraction,         lambda v: f"{round(v*100)}%"),
        ("cpuLimitMult",     rc.cpu_limit_multiplier,     brc.cpu_limit_multiplier,    lambda v: f"{v:.1f}x"),
        ("memLimitMult",     rc.memory_limit_multiplier,  brc.memory_limit_multiplier, lambda v: f"{v:.1f}x"),
        ("growOnly",         eff.grow_only,               base.grow_only,              str),
        ("shrinkOnly",       eff.shrink_only,             base.shrink_only,            str),
        ("createMr",         eff.create_mr,               base.create_mr,              str),
        ("dryRun",           eff.dry_run,                 base.dry_run,                str),
    ]:
        if val != dval:
            parts.append(f"{name}={fmt(val)}")
    if parts:
        _log.info("    %s/%s [override]: %s", rec.namespace, rec.target_name, "  ".join(parts))


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #

def cmd_check_prometheus(_args: argparse.Namespace) -> int:
    cfg = Config.load()
    _log_module.setup(level=cfg.log_level, fmt=cfg.log_format, color=cfg.log_color)
    load_config()

    if not cfg.prometheus_url:
        _log.warning("No Prometheus URL configured (set config.prometheusUrl in chart values).")
        return 1
    _resolve_prometheus_url(cfg)
    status = check_connectivity(cfg.prometheus_url)
    level = _log.info if status.startswith("OK") else _log.warning
    level("[check] %s: %s", cfg.prometheus_url, status)
    return 0 if status.startswith("OK") else 1


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _log_module.setup(level=cfg.log_level, fmt=cfg.log_format, color=cfg.log_color)

    # Merge CLI flag overrides into cfg BEFORE validate() so the effective
    # state is what validate() sees.  The --mr flag overrides the helm-level
    # createMr default; if the ConfigMap has createMr=False (the safe
    # default) and the operator passes --mr without a token, validate()
    # would have passed on the pre-mutation state and the first MR attempt
    # would crash with 401 after the push already committed.
    if args.mr is not None:
        cfg.create_mr = args.mr

    # Fail fast on missing required config (CR write-back target). The chart's
    # `required` template enforces this at install time too — this re-check
    # protects against a hand-edited ConfigMap drifting past that gate.
    cfg.validate()

    log_git_credentials_state(
        repo_url=cfg.cr_writeback.repo_url,
        git_token=cfg.git_token,
        git_provider=cfg.git_provider,
        git_username=cfg.git_username,
    )
    _log_global_config(cfg)

    load_config()

    # `[prometheus]` log line stays in the pre-phase context so it lands
    # with [git] / [config] as part of the startup preamble. The URL must
    # already be set in cfg (Config.validate enforces this at startup; the
    # chart's validate.yaml mirrors the check at helm-install time).
    _resolve_prometheus_url(cfg)

    # Three explicit phases — `phase_ctx` sets the JSON `phase` field on every
    # record emitted inside the block. The text-mode banner (a colored
    # capitalized word) is emitted by `log_phase_banner` at the top of each
    # block so the operator sees one transition marker per phase instead of
    # a per-line `[<phase>]` prefix.
    # Listing happens BEFORE the banner so the banner can include
    # `(N ns, M wl)` as a subtitle — the operator sees the cluster's
    # opt-in shape in one glance instead of having to count the listed
    # `=== ns ===` blocks. The contextvar is set inside the with-block
    # so the JSON `phase=discovery` field still applies to all the
    # listing log lines below (the banner itself is emitted from inside
    # the block too).
    with _log_module.phase_ctx("discovery"):
        # 1. Find namespaces opted in via annotation.
        targets = list_enabled_namespaces()
        if not targets:
            _log_module.log_phase_banner(_log, "discovery")
            _log.info("No namespaces with annotation 'kube-resource-updater.enabled: \"true\"'.")
            _log.info("Add it to a Namespace to opt in:")
            _log.info("  metadata.annotations: kube-resource-updater.enabled: \"true\"")
            return 0

        # 2. List workloads in each opted-in namespace.
        apps_api = client.AppsV1Api()
        batch_api = client.BatchV1Api()
        workloads = list_workloads(apps_api, [t.name for t in targets], batch_api=batch_api)

        if not workloads:
            _log_module.log_phase_banner(_log, "discovery",
                                          subtitle=f"{len(targets)} ns, 0 wl")
            _log.info("Opted-in namespaces (%d) have no Deployments, StatefulSets, DaemonSets, or CronJobs.",
                      len(targets), extra={"namespace_count": len(targets), "workload_count": 0})
            return 0

        # Banner now that ns + wl counts are known.
        _log_module.log_phase_banner(
            _log, "discovery",
            subtitle=f"{len(targets)} ns, {len(workloads)} wl",
        )

        # 3. Resolve effective Config per workload, filter conflicts and skips.
        ns_by_name = {t.name: t for t in targets}
        pairs: list[tuple[WorkloadRecommendation, Config]] = []

        for i, ns in enumerate(sorted(ns_by_name)):
            ns_workloads = [w for w in workloads if w.namespace == ns]
            # Blank line BETWEEN namespace blocks, not BEFORE the first one
            # (the banner's trailing blank already supplies that separation
            # so emitting another here would produce a double-blank).
            if i > 0:
                _log.info("")
            _log.info("=== %s ===  (workloads=%d)", ns, len(ns_workloads),
                      extra={"namespace": ns, "workload_count": len(ns_workloads)})
            for rec in sorted(ns_workloads, key=lambda w: w.target_name):
                if is_workload_skipped(rec.annotations):
                    _log.info("[SKIP] %s/%s: kube-resource-updater.skip=true",
                              rec.namespace, rec.target_name,
                              extra={"namespace": rec.namespace, "workload": rec.target_name,
                                     "status": "skip", "reason": "annotation_skip"})
                    continue
                eff = resolve_for_workload(
                    cfg,
                    ns_by_name[rec.namespace].annotations,
                    rec.annotations,
                    namespace_name=rec.namespace,
                    workload_name=rec.target_name,
                )
                # Resolve skipContainers through the same hierarchy. Stashed on
                # `rec` so the writeback layer (which already accepts (rec, cfg)
                # tuples) can read it without a signature change. Init
                # containers were never on `rec.containers` to begin with, so no
                # explicit init filter is needed here.
                rec.skip_containers = resolve_skip_containers(
                    cfg.skip_containers,
                    ns_by_name[rec.namespace].annotations,
                    rec.annotations,
                )
                if rec.init_container_names:
                    _log.info(
                        "  %s/%s skipping init containers: %s",
                        rec.namespace, rec.target_name,
                        ", ".join(rec.init_container_names),
                    )
                # Note: growOnly + shrinkOnly used to SKIP the
                # workload here. That had a hidden cost: SKIP removes the
                # workload from `entries` → the CR file rebuild drops its doc
                # → ArgoCD prunes the CR → admission webhook stops patching
                # the workload's pods → pods fall back to deployment-spec
                # resources. Almost certainly NOT what the operator intended
                # when setting both flags to true — that combination reads as
                # "freeze this workload, don't change its values" not as
                # "stop managing this workload entirely." The workload now
                # flows through normally; _build_containers_payload writes a
                # CR identical to what's already in the apiserver (because
                # grow/shrink with old_res from apiserver returns old_res for
                # every field), the WARNING for freeze is logged there.
                _log.info("  %s  containers=%d", rec.target_name, len(rec.containers),
                          extra={"namespace": rec.namespace, "workload": rec.target_name,
                                 "container_count": len(rec.containers)})
                _log_effective_overrides(rec, eff, cfg)
                pairs.append((rec, eff))

        if not pairs:
            _log.info("No eligible workloads after annotation filtering.")
            return 0

        if not cfg.prometheus_url:
            # Defence-in-depth: Config.validate at startup AND the chart's
            # validate.yaml at render-time both gate this. Reaching here
            # means a hand-edited ConfigMap drifted past both — log and
            # exit non-zero so the CronJob's restartPolicy doesn't paper
            # over the misconfiguration.
            _log.error(
                "[prometheus] config.prometheusUrl is empty — cannot compute "
                "recommendations. Set it in the chart values; the chart's "
                "validate.yaml normally catches this at helm install time."
            )
            return 1

    with _log_module.phase_ctx("recommend"):
        _log_module.log_phase_banner(_log, "recommend")
        # 4. OOM-aware. When enabled, fetch the live state
        # (annotations from existing CRs) AND scan pods for fresh OOM events.
        # Per-workload eligibility resolves through the same hierarchy as the
        # other annotation-driven knobs (helm < ns < workload).
        oom_state_lookup: dict = {}
        oom_events_lookup: dict = {}
        oom_eligibility_lookup: dict = {}
        oom_floor_enabled_lookup: dict = {}
        oom_floor_reset_lookup: dict = {}
        if cfg.resource.oom_detection_enabled:
            from src.writeback_webhook import detect_oom_events, fetch_oom_state
            from src.overrides import (
                is_oom_detection_enabled,
                is_oom_floor_enabled,
                is_oom_floor_reset_requested,
            )

            opt_in_namespaces = [t.name for t in targets]
            co_api = client.CustomObjectsApi()
            oom_state_lookup = fetch_oom_state(co_api, opt_in_namespaces)

            # Build the per-workload eligibility map upfront. Only workloads
            # whose effective resolver says YES participate in bumping;
            # those marked NO still respect existing floors (already
            # applied in `_build_containers_payload` regardless).
            helm_floor_default = cfg.resource.oom_floor_enabled
            for rec, _ in pairs:
                ns_anns = ns_by_name[rec.namespace].annotations
                eligible = is_oom_detection_enabled(True, ns_anns, rec.annotations)
                oom_eligibility_lookup[(rec.namespace, rec.target_name)] = eligible
                oom_floor_enabled_lookup[(rec.namespace, rec.target_name)] = (
                    is_oom_floor_enabled(helm_floor_default, ns_anns, rec.annotations)
                )
                oom_floor_reset_lookup[(rec.namespace, rec.target_name)] = (
                    is_oom_floor_reset_requested(ns_anns, rec.annotations)
                )

            # Detection: scan pods. Limit to workloads we actually process
            # (filters out non-Deployment/StatefulSet/DaemonSet pods).
            core_v1 = client.CoreV1Api()
            workload_keys = {(rec.namespace, rec.target_name) for rec, _ in pairs}
            oom_events_lookup = detect_oom_events(core_v1, opt_in_namespaces, workload_keys)
            if oom_events_lookup:
                _log.info(
                    "[oom] detected %d fresh OOM event(s) across %d workload(s); "
                    "memory will be bumped where dedupe says new.",
                    len(oom_events_lookup),
                    len({(ns, wl) for ns, wl, _ in oom_events_lookup}),
                )

        # 5. Per-namespace autoRollout decision for the MR description footer.
        # `is_auto_rollout_enabled(helm_default=False, ns_anns, {})` mirrors the
        # webhook's own resolver: helm-default is intentionally False so the
        # operator opts in per namespace via annotation. Workload-level overrides
        # are not surfaced here (per-namespace summary is more useful in the MR
        # description; per-workload still takes effect at admission).
        from src.overrides import is_auto_rollout_enabled
        auto_rollout_by_namespace = {
            t.name: is_auto_rollout_enabled(False, t.annotations, {})
            for t in targets
        }

        # 6. Hand off to the writeback layer. `create_mr` is no longer a global
        # parameter — each pair's `Config.create_mr` is the effective per-workload
        # value (resolver already merged helm < ns < workload). The writeback
        # buckets entries by `create_mr` and emits up to two pushes per repo:
        # one direct, one MR. The CLI flag `--mr` still flips
        # the helm-level default before resolution, kept for local dev parity.
        results = write_back_webhook_all(
            workloads_with_configs=pairs,
            cr_writeback=cfg.cr_writeback,
            provider=build_provider(
                repo_url=cfg.cr_writeback.repo_url,
                token=cfg.git_token,
                provider_override=cfg.git_provider,
                api_url=cfg.git_api_url,
                username=cfg.git_username,
            ),
            git_author_name=cfg.git_author_name,
            git_author_email=cfg.git_author_email,
            dry_run=cfg.dry_run,
            mr_config=cfg.mr,
            oom_state_lookup=oom_state_lookup,
            oom_events_lookup=oom_events_lookup,
            oom_eligibility_lookup=oom_eligibility_lookup,
            oom_floor_enabled_lookup=oom_floor_enabled_lookup,
            oom_floor_reset_lookup=oom_floor_reset_lookup,
            auto_rollout_by_namespace=auto_rollout_by_namespace,
        )

    if not results:
        return 0

    with _log_module.phase_ctx("result"):
        # Subtitle reflects how many MR/push tuples the writeback returned.
        # `len(results)` is normally 1 (single repo) but the architecture
        # supports multi-repo write-back so the count stays meaningful.
        n_mr = sum(
            1 for url, _ in results
            if "/merge_requests/" in url or "/-/merge_requests/" in url or "/pull/" in url
        )
        n_push = len(results) - n_mr
        parts = []
        if n_mr:
            parts.append(f"{n_mr} MR")
        if n_push:
            parts.append(f"{n_push} direct")
        subtitle = ", ".join(parts) if parts else None
        _log_module.log_phase_banner(_log, "result", subtitle=subtitle)
        for i, (url, namespaces) in enumerate(results):
            is_mr = "/merge_requests/" in url or "/-/merge_requests/" in url or "/pull/" in url
            label = "MR" if is_mr else "Pushed"
            # Same blank-between-not-before pattern as the discovery loop:
            # banner already supplies the leading separator.
            if i > 0:
                _log.info("")
            _log.info("%s: %s", label, url, extra={"result_type": label.lower(), "result_url": url})
            _log.info("-> namespaces: %s", ", ".join(namespaces))
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Run the mutation admission webhook.

    Long-running process: serves AdmissionReview requests from kube-apiserver and
    patches pod resources based on ResourceOverride CRs.

    Deployed via the same Helm chart as the sync CronJob, gated by `webhook.enabled`.

    Cert lifecycle: the chart no longer depends on cert-manager. An in-process
    cert reconciler (src/webhook_cert.py) generates a self-signed CA + serving
    cert on first start, writes them to the cert-dir for aiohttp, patches the
    MutatingWebhookConfiguration's caBundle, and rotates ahead of expiry.
    Invoked here before the aiohttp server binds — `run_once_blocking()` only
    returns once the cert materials exist on disk.
    """
    cfg = Config.load()
    _log_module.setup(level=cfg.log_level, fmt=cfg.log_format, color=cfg.log_color)
    load_config()

    from src.k8s import custom_objects_api
    from src.webhook_cert import CertReconciler
    from src.webhook_server import run as run_webhook

    # The webhook RBAC + chart wire all of these up; defaults match the chart
    # naming so a hand-run `python main.py webhook` outside the chart still
    # works against a chart-deployed Service/MWC.
    secret_name = os.environ.get("WEBHOOK_CERT_SECRET", "kube-resource-updater-webhook-cert")
    service_name = os.environ.get("WEBHOOK_SERVICE", "kube-resource-updater-webhook")
    mwc_name = os.environ.get("WEBHOOK_MWC", "kube-resource-updater-webhook")
    # VWC is optional — empty string when the chart did not enable
    # webhook.validating.enabled. The reconciler short-circuits empty.
    vwc_name = os.environ.get("WEBHOOK_VWC", "")
    namespace = (
        os.environ.get("POD_NAMESPACE")
        or _read_pod_namespace()
        or "kube-resource-updater"
    )
    # Cluster domain configurable so the webhook cert SAN
    # matches whatever DNS suffix the apiserver dials. Default
    # `cluster.local` keeps every existing deployment working unchanged;
    # clusters with custom `clusterDomain` set the chart value and the
    # template wires it through this env var.
    cluster_domain = os.environ.get("WEBHOOK_CLUSTER_DOMAIN", "cluster.local")

    reconciler = CertReconciler(
        secret_name=secret_name,
        namespace=namespace,
        service_name=service_name,
        webhook_configuration_name=mwc_name,
        validating_webhook_configuration_name=vwc_name,
        cluster_domain=cluster_domain,
        cert_dir=args.cert_dir,
    )
    reconciler.run_once_blocking()
    reconciler.start()

    api = custom_objects_api()
    # Status writer flags come from env vars set by the chart's webhook
    # Deployment (downward from `webhook.status.*` in values). Defaults
    # mirror the chart defaults so a hand-run `python main.py webhook`
    # outside the chart still gets sensible behaviour.
    status_enabled = os.environ.get("WEBHOOK_STATUS_ENABLED", "true").lower() in ("1", "true", "yes")
    try:
        status_flush = int(os.environ.get("WEBHOOK_STATUS_FLUSH_INTERVAL_SECONDS", "30"))
    except ValueError:
        status_flush = 30
    auto_rollout_enabled = os.environ.get("WEBHOOK_AUTO_ROLLOUT_ENABLED", "false").lower() in ("1", "true", "yes")
    try:
        auto_rollout_debounce = int(os.environ.get("WEBHOOK_AUTO_ROLLOUT_DEBOUNCE_SECONDS", "30"))
    except ValueError:
        auto_rollout_debounce = 30
    run_webhook(
        port=args.port,
        cert_dir=args.cert_dir,
        metrics_port=args.metrics_port,
        custom_objects_api=api,
        status_enabled=status_enabled,
        status_flush_interval_seconds=status_flush,
        auto_rollout_enabled=auto_rollout_enabled,
        auto_rollout_debounce_seconds=auto_rollout_debounce,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kube-resource-updater",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("check-prometheus", help="Test connectivity to the configured / discovered Prometheus")
    sync_parser = sub.add_parser("sync", help="Write resource recommendations to Git")
    sync_parser.add_argument(
        "--mr", action="store_true", default=None,
        help="Open a GitLab MR instead of pushing directly to the target branch",
    )
    webhook_parser = sub.add_parser(
        "webhook",
        help="Run the mutation admission webhook (long-running). Reads ResourceOverride CRs from the cluster.",
    )
    webhook_parser.add_argument(
        "--port", type=int, default=9443,
        help="TLS port for AdmissionReview requests (default: 9443)",
    )
    webhook_parser.add_argument(
        "--cert-dir", default="/tmp/k8s-webhook-server/serving-certs",
        help="Directory holding tls.crt and tls.key (cert-manager mount target)",
    )
    webhook_parser.add_argument(
        "--metrics-port", type=int, default=8080,
        help="Plain HTTP port for /healthz, /readyz, /metrics (default: 8080)",
    )

    args = parser.parse_args()
    _log_module.setup()  # bootstrap with env var defaults before config is loaded

    dispatch = {
        "check-prometheus": cmd_check_prometheus,
        "sync":             cmd_sync,
        "webhook":          cmd_webhook,
    }
    if args.command not in dispatch:
        parser.print_help()
        return 1

    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
