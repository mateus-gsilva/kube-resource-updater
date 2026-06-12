"""Kubernetes API helpers — local cluster only.

The Prometheus URL is configured explicitly via `config.prometheusUrl` since
chart 1.20.0 — the previous auto-discovery (Service DNS + Prometheus CR
fallback) was removed. Two reasons:

  - Operators outside the kube-prometheus-stack convention (Prometheus in
    a namespace other than `monitoring`, or behind an ingress) had to set
    the URL explicitly anyway; the discovery code path was effectively
    dead for them.
  - Auto-discovery added two cluster-wide RBAC grants (endpoints/services
    + Prometheus CR `list/get`) that audit pipelines kept flagging.
    Removing the code drops the grants entirely.

What stays in this module:
  1. Bootstrap the in-cluster k8s client (`load_config`, `custom_objects_api`).

Namespace discovery (the opt-in path) lives in `src/discovery.py`. Git
credentials come from the `GITLAB_TOKEN` env var only (chart populates it via
`gitlab.token` or `gitlab.existingSecret`); the ArgoCD repo-Secret fallback
was removed in 1.2.0.
"""
from kubernetes import client, config

from . import log as _log_module

_log = _log_module.get(__name__)


def load_config() -> None:
    """Load k8s config: in-cluster first, then kubeconfig."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def custom_objects_api() -> client.CustomObjectsApi:
    return client.CustomObjectsApi()
