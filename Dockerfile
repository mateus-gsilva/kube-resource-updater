# Base pinned by digest, not just the mutable `3.12-slim-bookworm` tag, so a
# rebuild months from now resolves the identical base layer — same failure
# class as the 2026-05-28 arm64 outage (a mutable tag silently changed under
# us). Digest is the multi-arch index (amd64 + arm64). Refresh it deliberately
# when bumping Python; resolve with `docker buildx imagetools inspect`. (#34)
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS builder

WORKDIR /build
# Install the fully-resolved, exact-pinned dependency set (requirements.lock,
# regenerated from requirements.txt via uv — see that file's header) so the
# image is reproducible across builds and can't drift onto an untested
# transitive version. (#33)
COPY requirements.lock .
RUN pip install --no-cache-dir --target=/deps -r requirements.lock

FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d

# Pull in the latest Debian security patches at build time so the image
# ships with current openssl / libssl / zlib / etc. The legacy `helm`
# binary that used to live here was dropped in chart 1.20.1 — the runtime
# Python code never shelled out to it (helm-write-back was deleted in
# chart 1.0.0 when the project switched to the ResourceOverride CRD).
# Removing it killed ~21 Go-stdlib CVEs trivy was flagging on every scan.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /deps /deps
COPY src/ ./src/
COPY main.py .

RUN useradd -u 1001 -r -g 0 -s /sbin/nologin appuser
USER 1001

ENV PYTHONPATH=/deps

ENTRYPOINT ["python", "main.py"]
