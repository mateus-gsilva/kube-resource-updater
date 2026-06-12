# Contributing to kube-resource-updater

Thanks for your interest. This document is the working contract between
maintainers and contributors — what we expect of patches, what we'll do with
them, and how to set up locally.

## Quick start

```bash
git clone <repo-url>
cd kube-resource-updater
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest + ruff (dev tooling only)

# QA suite — must be green before any PR (~1,200 asserts).
python3 tools/qa_params.py
# Equivalent thin alias (CI runs this): pytest
pytest

# Lint (config in pyproject.toml; correctness rules only).
ruff check src/ tools/ main.py tests/

# Dry-run sync end-to-end against a real Prometheus (no git writes).
# Requires a .env file with PROMETHEUS_URL and CR_WRITEBACK_REPO_URL set;
# see README.md → "Local development".
DRY_RUN=true python3 main.py sync
```

## What we accept

- **Bug fixes** with a regression test in `tools/qa_params.py`. The PR
  description should name the bug class (silent fall-through, off-by-one,
  RBAC drift, etc.) and reference the failing scenario.
- **Features that align with the [ROADMAP](ROADMAP.md)**. Open an issue
  first if the design isn't obviously a follow-up to an existing item —
  cheaper to disagree before code is written.
- **Documentation improvements** — `docs/reference.md` is the user-facing
  source of truth; the README is the operator entry point. Drift between
  the two is a bug.

## What we don't accept (without a strong case)

- New cluster-wide RBAC grants. The chart's RBAC has been tightened down
  to namespace-scoped Roles wherever possible; new ClusterRole rules need
  to justify why a Role won't work.
- New top-level config keys when an annotation-resolver path already
  exists. Per-workload / per-namespace overrides resolve through
  `src/overrides.py` — extend the resolver chain instead.
- Helm/Kustomize path-detection heuristics for the legacy write-back
  path. That path is going away; new code should target the
  `ResourceOverride` CRD only.

## Code style

- **Python**: PEP 8, type hints on every new function (PEP 604 unions
  `int | None` style — minimum Python 3.10), and `ruff`-clean. Comments
  explain *why*, not *what* — assume the reader knows Python.
- **Helm templates**: comment every conditional gate with the value that
  toggles it and the reason. New templates go through
  `templates/validate.yaml` for any cross-value invariants.
- **Log lines** follow the chart 1.19.0+ palette / banner / phase
  convention: tag-first, structured `extra={...}` for JSON consumers,
  blank lines between blocks, no leading whitespace dependence (Argo CD's
  log viewer strips it).

## Tests are the contract

Every code change should produce a corresponding QA assert. We grade PRs
on "would the QA catch this regression on the next bisect?" — the answer
must be yes. `tools/qa_params.py` currently runs ~1,200 asserts across ~68
sections (see the SECTION INDEX at the top of the file); per-feature
sections are the model to follow when adding new functionality.

The harness is deliberately custom rather than pytest-native: asserts are
written fail-first (they must FAIL against the pre-fix code before the fix
lands), the whole suite always runs to completion with a failure counter
instead of stopping at the first error (one bisect run shows every
regression at once), and it has zero test-framework dependencies — the
container image can run it as-is. `pytest` still works as a thin alias:
`tests/test_qa_suite.py` shells out to the canonical script, and the
overrides unit-test file (`tools/test_overrides.py`) is bridged in as a
section, so `python3 tools/qa_params.py` is the single entrypoint.

Live-test changes against a real cluster before opening a PR if they
touch:

- Admission webhook code paths (mutation, validation, cert reconciler).
- Auto-rollout watcher (workload patch on CR change).
- OOM detection (`OOMKilled` event scanning).
- Git writeback (clone, push, MR creation).

The QA mocks the apiserver but doesn't simulate the full apiserver +
webhook + kubelet loop; live tests catch sequencing and label-selector
bugs that unit tests can't see.

## Commit messages

Conventional Commits is the format. Examples:

```
log: split [OK] webhook → [mr] opened / [push] for per-repo result lines
chart: gate WEBHOOK_STATUS_FLUSH_INTERVAL_SECONDS on status.enabled
fix: shrinkOnly was reverting OOM bumps without [oom-bump-suppressed] warning
```

Body wrapped to 72 columns, references the relevant ROADMAP item or
issue. No co-author trailers for AI-assisted commits — judgment call,
not a hard rule.

## Release process

1. QA green locally: `python3 tools/qa_params.py`
2. Bump `gitops/helm-charts/kube-resource-updater/Chart.yaml` (both
   `version` and `appVersion`).
3. Add a release entry to `CHANGELOG.md` (date, commit, chart tag, image
   digest, cluster commit, live-test result) — `ROADMAP.md` stays scoped to
   *pending* work; close items move out of it into the CHANGELOG entry.
4. Commit, push, tag the chart repo with `<version>`.
5. Update the target environment's GitOps Application `targetRevision` to
   the new tag.

There is no "main" / "develop" branching strategy yet — single `main`,
fast-forward merges only.

## License

Apache License 2.0. See [LICENSE](LICENSE). Contributions are accepted
under the same license; new files should carry no source-header
boilerplate unless required by a downstream consumer.
