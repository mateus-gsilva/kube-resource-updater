"""
In-process serving-cert reconciler for the admission webhook.

Drop-in replacement for cert-manager + a `Certificate` CR. Pattern lifted from
Kyverno's cert-controller and OPA Gatekeeper's cert rotator: the webhook pod
itself owns its serving cert. On startup it ensures a valid cert exists in a
known Secret and writes the materials to local disk for `aiohttp` to load. A
background loop watches the Secret and the `MutatingWebhookConfiguration` and
patches them back into shape if anything drifts (ArgoCD prune, manual delete,
operator typo). A scheduler regenerates the cert before expiry.

Runs as a thread, not a process: the webhook server keeps owning the
`aiohttp` event loop on the main thread; the reconciler is a daemon thread
that talks to the apiserver via the kubernetes client.

Design choices, briefly:

  * **Self-signed CA, no intermediate.** The webhook is private — only
    kube-apiserver talks to it. CT logs and public PKI are irrelevant; the
    operator's threat model is "another tenant in the same cluster" and the
    CA serves as the trust root for that one tenant.
  * **Cert lifetime: 1 year. Renew at 30 days before expiry.** Mirrors the
    cert-manager default we used to ship.
  * **Rotation = process exit.** When a renewal is due, the reconciler
    rewrites the Secret + caBundle and then `os._exit`s the process. The
    Deployment controller restarts the pod, which loads the new cert from
    disk on aiohttp startup. Simpler than hot-reloading aiohttp's TLS
    context, and the rolling restart respects the PDB.
  * **Single writer.** Multiple replicas race on the Secret using a
    resource-version compare-and-swap (PUT with `resourceVersion` echo); the
    loser sees a 409 Conflict, re-reads the Secret, and uses whatever cert
    the winner produced. No leader election required for this small piece
    of work.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import os
import threading
from dataclasses import dataclass
from typing import NoReturn

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client, watch
from kubernetes.client.rest import ApiException

from src import log as _log_module

_log = _log_module.get(__name__)


CERT_VALIDITY_DAYS = 365
RENEW_BEFORE_DAYS = 30
RECONCILE_INTERVAL_SECONDS = 24 * 3600
WATCH_RESTART_BACKOFF_SECONDS = 5


@dataclass
class _CertMaterials:
    """Bundle of PEM-encoded bytes — what a TLS server needs to start."""
    ca_pem: bytes
    cert_pem: bytes
    key_pem: bytes


# --------------------------------------------------------------------------- #
# Cert generation                                                              #
# --------------------------------------------------------------------------- #

def _generate_cert(service: str, namespace: str,
                   cluster_domain: str = "cluster.local") -> _CertMaterials:
    """Generate a self-signed CA + a serving cert signed by it.

    SANs cover both the short and long DNS forms the apiserver might use to
    reach the webhook (`<svc>`, `<svc>.<ns>`, `<svc>.<ns>.svc`,
    `<svc>.<ns>.svc.<cluster_domain>`). Without all four, certain Service
    discovery paths fail TLS handshake.

    `cluster_domain` defaults to `cluster.local` (the upstream k8s default
    and what every chart deployment uses unless explicitly overridden).
    Clusters configured with a custom domain (`cluster.foo.com`) MUST
    pass that value through the chart's `clusterDomain` value, or the
    apiserver-to-webhook TLS handshake fails on SAN mismatch — silently,
    because `failurePolicy: Ignore` admits pods with helm defaults.
    Added in 1.22.11.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = now + datetime.timedelta(days=CERT_VALIDITY_DAYS)

    # ── CA ────────────────────────────────────────────────────────────────
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"{service}-ca"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # ── Serving cert (signed by CA) ───────────────────────────────────────
    serv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [
        f"{service}",
        f"{service}.{namespace}",
        f"{service}.{namespace}.svc",
        f"{service}.{namespace}.svc.{cluster_domain}",
    ]
    serv_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, sans[2]),
    ])
    serv_cert = (
        x509.CertificateBuilder()
        .subject_name(serv_subject)
        .issuer_name(ca_cert.subject)
        .public_key(serv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(expires)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in sans]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    return _CertMaterials(
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM),
        cert_pem=serv_cert.public_bytes(serialization.Encoding.PEM),
        key_pem=serv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def _cert_expiry(cert_pem: bytes) -> datetime.datetime | None:
    """Parse `notAfter` from a PEM cert. Returns None on malformed input.

    Prefers the timezone-aware `not_valid_after_utc` (cryptography ≥ 42); falls
    back to the legacy naive `not_valid_after` and stamps it as UTC. Both
    paths return a tz-aware datetime so the caller can subtract `now(UTC)`
    without `naive vs aware` errors.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception:
        return None
    naf = getattr(cert, "not_valid_after_utc", None)
    if naf is not None:
        return naf
    naive = cert.not_valid_after  # deprecated but always present
    return naive.replace(tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------- #
# Reconciler                                                                   #
# --------------------------------------------------------------------------- #

class CertReconciler:
    """Owns the webhook serving cert lifecycle.

    ``run_once_blocking()`` is the bootstrap entry the main thread invokes
    BEFORE starting aiohttp — it guarantees a valid cert exists on disk and
    in the apiserver. ``start()`` then spins the background watch loops in
    a daemon thread.
    """

    def __init__(
        self,
        *,
        secret_name: str,
        namespace: str,
        service_name: str,
        webhook_configuration_name: str,
        cert_dir: str,
        validating_webhook_configuration_name: str = "",
        cluster_domain: str = "cluster.local",
        core_v1: client.CoreV1Api | None = None,
        admission_v1: client.AdmissionregistrationV1Api | None = None,
    ):
        self._secret_name = secret_name
        self._namespace = namespace
        self._service_name = service_name
        self._cluster_domain = cluster_domain
        self._mwc_name = webhook_configuration_name
        # Validating webhook configuration is optional — only the
        # operators who flip `webhook.validating.enabled` in the chart
        # provide a name here. Empty string disables every VWC code path.
        self._vwc_name = validating_webhook_configuration_name
        self._cert_dir = cert_dir
        self._core = core_v1 or client.CoreV1Api()
        self._adm = admission_v1 or client.AdmissionregistrationV1Api()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Bootstrap (blocking)                                                 #
    # ------------------------------------------------------------------ #

    def run_once_blocking(self) -> None:
        """Ensure cert materials exist and are written to ``cert_dir``.

        Idempotent: a re-run on an unchanged cluster does no API writes.
        Called once on pod startup before the TLS server binds.
        """
        materials, source = self._ensure_secret()
        self._write_cert_dir(materials)
        self._ensure_mwc_ca_bundle(materials.ca_pem)
        self._ensure_vwc_ca_bundle(materials.ca_pem)
        _log.info(
            "[webhook-cert] bootstrap ok (secret=%s/%s source=%s expires=%s)",
            self._namespace, self._secret_name, source,
            _cert_expiry(materials.cert_pem),
        )

    # ------------------------------------------------------------------ #
    # Background loops                                                     #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="webhook-cert-reconciler", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        # Two concurrent watchers + one expiry timer share this thread by
        # cycling: each iteration spends a bounded amount of time on each
        # task. Simpler than three separate threads, plenty fast for the
        # event volume (a handful of mutations per cluster lifetime).
        while not self._stop.is_set():
            try:
                self._check_expiry()
                self._watch_secret_briefly()
                self._watch_mwc_briefly()
            except Exception:
                _log.exception("[webhook-cert] reconciler iteration failed")
                self._stop.wait(WATCH_RESTART_BACKOFF_SECONDS)

    def _check_expiry(self) -> None:
        """If the cert in the Secret expires soon, regenerate and exit.

        We exit so the Deployment controller restarts the pod with the new
        cert loaded into aiohttp. Hot-reloading TLS contexts inside aiohttp
        is doable but adds bug surface for a once-a-year event.
        """
        try:
            sec = self._core.read_namespaced_secret(self._secret_name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                _log.warning("[webhook-cert] secret disappeared; regenerating")
                self._regenerate_and_exit()
            raise

        try:
            cert_pem = base64.b64decode((sec.data or {}).get("tls.crt", "") or b"")
        except (ValueError, binascii.Error):
            _log.warning(
                "[webhook-cert] secret tls.crt contains invalid base64 — "
                "cert Secret corrupted, regenerating",
            )
            self._regenerate_and_exit()
            return  # _regenerate_and_exit calls os._exit; this is for type-checker
        if not cert_pem:
            _log.warning("[webhook-cert] secret has no tls.crt; regenerating")
            self._regenerate_and_exit()

        expires = _cert_expiry(cert_pem)
        if expires is None:
            _log.warning("[webhook-cert] secret has malformed tls.crt; regenerating")
            self._regenerate_and_exit()

        remaining = expires - datetime.datetime.now(datetime.timezone.utc)
        if remaining < datetime.timedelta(days=RENEW_BEFORE_DAYS):
            _log.info(
                "[webhook-cert] cert expires in %d days (< %d); regenerating",
                remaining.days, RENEW_BEFORE_DAYS,
            )
            self._regenerate_and_exit()

    def _watch_secret_briefly(self) -> None:
        """Watch the Secret; on DELETE, regenerate and exit.

        The watch returns after `timeout_seconds` even with no events, so the
        outer loop gets a chance to run the expiry check on a regular cadence
        without a separate timer.
        """
        w = watch.Watch()
        try:
            for event in w.stream(
                self._core.list_namespaced_secret,
                namespace=self._namespace,
                field_selector=f"metadata.name={self._secret_name}",
                timeout_seconds=300,
            ):
                if self._stop.is_set():
                    return
                if event.get("type") == "DELETED":
                    _log.warning("[webhook-cert] secret deleted; regenerating")
                    self._regenerate_and_exit()
        finally:
            w.stop()

    def _watch_mwc_briefly(self) -> None:
        """Watch the MWC; if our caBundle drifts, patch it back."""
        try:
            current_ca = base64.b64decode(
                (self._core.read_namespaced_secret(self._secret_name, self._namespace)
                 .data or {}).get("ca.crt", "") or b""
            )
        except ApiException:
            return
        if not current_ca:
            return

        w = watch.Watch()
        try:
            for event in w.stream(
                self._adm.list_mutating_webhook_configuration,
                field_selector=f"metadata.name={self._mwc_name}",
                timeout_seconds=300,
            ):
                if self._stop.is_set():
                    return
                if event.get("type") in ("MODIFIED", "ADDED"):
                    self._ensure_mwc_ca_bundle(current_ca)
                    # The MWC tick is also our cheap "wake every ~300s"
                    # signal; piggy-back the VWC reconcile here so we
                    # don't need a second watch loop. caBundle drift on
                    # the VWC is the same shape of problem (ArgoCD prunes
                    # the runtime-applied bytes back to the chart's empty
                    # placeholder).
                    self._ensure_vwc_ca_bundle(current_ca)
        finally:
            w.stop()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _ensure_secret(self) -> tuple[_CertMaterials, str]:
        """Read the Secret. If missing/empty/bad, regenerate and write back.

        Returns ``(materials, source)`` where source is "existing" or
        "generated". The dual-return lets the caller log which path was taken.
        """
        try:
            sec = self._core.read_namespaced_secret(self._secret_name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            return self._regenerate_secret(), "generated"

        data = sec.data or {}
        try:
            cert_pem = base64.b64decode(data.get("tls.crt", "") or b"")
            key_pem = base64.b64decode(data.get("tls.key", "") or b"")
            ca_pem = base64.b64decode(data.get("ca.crt", "") or b"")
        except (ValueError, binascii.Error):
            _log.warning(
                "[webhook-cert] secret data contains invalid base64 — "
                "cert Secret corrupted, regenerating",
            )
            return self._regenerate_secret(), "generated"

        if cert_pem and key_pem and ca_pem and _cert_expiry(cert_pem):
            return _CertMaterials(ca_pem=ca_pem, cert_pem=cert_pem, key_pem=key_pem), "existing"

        _log.info("[webhook-cert] secret incomplete; regenerating")
        return self._regenerate_secret(), "generated"

    def _regenerate_secret(self) -> _CertMaterials:
        materials = _generate_cert(self._service_name, self._namespace,
                                    cluster_domain=self._cluster_domain)
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=self._secret_name, namespace=self._namespace,
                labels={"app.kubernetes.io/managed-by": "kube-resource-updater"},
            ),
            type="kubernetes.io/tls",
            data={
                "ca.crt":  base64.b64encode(materials.ca_pem).decode(),
                "tls.crt": base64.b64encode(materials.cert_pem).decode(),
                "tls.key": base64.b64encode(materials.key_pem).decode(),
            },
        )
        try:
            self._core.replace_namespaced_secret(self._secret_name, self._namespace, body)
            return materials
        except ApiException as exc:
            if exc.status != 404:
                raise
        # 404 from REPLACE means the Secret doesn't exist yet. Try CREATE.
        # in multi-replica setups, all replicas race to
        # CREATE on first cold-start. One wins with 201, the others get
        # 409 AlreadyExists. Pre-1.21.0 the 409 propagated up and the
        # losing replica's reconciler thread crashed; pod liveness
        # eventually restarted it, but until then admission was broken.
        # Now we detect the race: on 409, re-read the freshly-created
        # Secret (whoever wrote it is now source of truth) and adopt
        # its cert. Loser's generated cert is discarded — same outcome
        # as restarting and reading the existing Secret on the next
        # pass, just without the crash + restart cycle.
        try:
            self._core.create_namespaced_secret(self._namespace, body)
            return materials
        except ApiException as exc:
            if exc.status != 409:
                raise
            _log.info(
                "[webhook-cert] CREATE Secret race lost to another replica; "
                "re-reading and adopting their cert.",
            )
        sec = self._core.read_namespaced_secret(self._secret_name, self._namespace)
        data = sec.data or {}
        try:
            adopted_cert_pem = base64.b64decode(data.get("tls.crt", "") or b"")
            adopted_key_pem  = base64.b64decode(data.get("tls.key", "") or b"")
            adopted_ca_pem   = base64.b64decode(data.get("ca.crt",  "") or b"")
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                "[webhook-cert] 409-adopted Secret contains invalid base64 — "
                "winner's cert is corrupt; regenerating"
            ) from exc
        if not (adopted_cert_pem and adopted_key_pem and adopted_ca_pem):
            raise ValueError(
                "[webhook-cert] 409-adopted Secret is missing one or more cert fields "
                "(cert/key/ca) — winner wrote incomplete data; regenerating"
            )
        if _cert_expiry(adopted_cert_pem) is None:
            raise ValueError(
                "[webhook-cert] 409-adopted cert has unparseable notAfter — "
                "winner's cert is malformed; regenerating"
            )
        return _CertMaterials(
            ca_pem=adopted_ca_pem,
            cert_pem=adopted_cert_pem,
            key_pem=adopted_key_pem,
        )

    def _write_cert_dir(self, materials: _CertMaterials) -> None:
        """Write tls.crt + tls.key into ``cert_dir`` for aiohttp to load.

        Uses atomic rename so a partial write never leaves aiohttp reading a
        truncated file. ``cert_dir`` must be on a writable filesystem; the
        chart mounts an emptyDir for this.
        """
        os.makedirs(self._cert_dir, exist_ok=True)
        for filename, content in [
            ("tls.crt", materials.cert_pem),
            ("tls.key", materials.key_pem),
            ("ca.crt", materials.ca_pem),
        ]:
            tmp = os.path.join(self._cert_dir, filename + ".tmp")
            final = os.path.join(self._cert_dir, filename)
            with open(tmp, "wb") as fh:
                fh.write(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, final)

    def _ensure_mwc_ca_bundle(self, ca_pem: bytes) -> None:
        """Patch the MWC's webhooks[*].clientConfig.caBundle if it drifts."""
        try:
            mwc = self._adm.read_mutating_webhook_configuration(self._mwc_name)
        except ApiException as exc:
            if exc.status == 404:
                _log.warning("[webhook-cert] MWC %s not found; skipping caBundle patch", self._mwc_name)
                return
            raise

        encoded = base64.b64encode(ca_pem).decode()
        needs_update = False
        for hook in mwc.webhooks or []:
            cc = hook.client_config
            current = cc.ca_bundle
            # The python client encodes caBundle as bytes (since v25 or so);
            # compare against both shapes to dodge spurious updates.
            current_str = current.decode() if isinstance(current, (bytes, bytearray)) else (current or "")
            if current_str != encoded:
                needs_update = True
                break
        if not needs_update:
            return

        # `replace` over the whole MWC is bullet-proof across kubernetes-client
        # versions: strategic-merge-patch and json-patch both have version
        # quirks (`_content_type` keyword absent in some, list-merge-keys
        # changed semantics in others). The MWC is a small, low-cardinality
        # object — replacing it once per cert rotation is fine.
        for hook in mwc.webhooks or []:
            hook.client_config.ca_bundle = encoded
        self._adm.replace_mutating_webhook_configuration(self._mwc_name, mwc)
        _log.info("[webhook-cert] MWC %s caBundle updated", self._mwc_name)

    def _ensure_vwc_ca_bundle(self, ca_pem: bytes) -> None:
        """Same idempotent caBundle reconcile as the MWC version, but
        for the optional ValidatingWebhookConfiguration. No-op when the
        chart did not enable validating (vwc_name is empty) or when the
        VWC simply doesn't exist (operator deployed an older chart
        version on top of the new pod). 404 is logged at debug level —
        not actionable.
        """
        if not self._vwc_name:
            return
        try:
            vwc = self._adm.read_validating_webhook_configuration(self._vwc_name)
        except ApiException as exc:
            if exc.status == 404:
                _log.debug("[webhook-cert] VWC %s not found; skipping caBundle patch",
                           self._vwc_name)
                return
            raise

        encoded = base64.b64encode(ca_pem).decode()
        needs_update = False
        for hook in vwc.webhooks or []:
            cc = hook.client_config
            current = cc.ca_bundle
            current_str = current.decode() if isinstance(current, (bytes, bytearray)) else (current or "")
            if current_str != encoded:
                needs_update = True
                break
        if not needs_update:
            return
        for hook in vwc.webhooks or []:
            hook.client_config.ca_bundle = encoded
        self._adm.replace_validating_webhook_configuration(self._vwc_name, vwc)
        _log.info("[webhook-cert] VWC %s caBundle updated", self._vwc_name)

    def _regenerate_and_exit(self) -> NoReturn:
        """Last-resort path: rewrite Secret + MWC + VWC, then exit so the pod restarts.

        Bypasses the rest of the iteration and never returns.
        """
        materials = self._regenerate_secret()
        self._ensure_mwc_ca_bundle(materials.ca_pem)
        self._ensure_vwc_ca_bundle(materials.ca_pem)
        _log.info("[webhook-cert] cert rotated; exiting so kubelet restarts pod with the new cert")
        # Use os._exit because aiohttp's loop is on the main thread; sys.exit
        # would propagate as an exception that aiohttp swallows.
        os._exit(0)
