{{/*
Expand the name of the chart.
*/}}
{{- define "kube-resource-updater.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "kube-resource-updater.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label.
*/}}
{{- define "kube-resource-updater.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels — applied to every resource.
*/}}
{{- define "kube-resource-updater.labels" -}}
helm.sh/chart: {{ include "kube-resource-updater.chart" . }}
{{ include "kube-resource-updater.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels — used in matchLabels.
*/}}
{{- define "kube-resource-updater.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kube-resource-updater.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Common annotations.
*/}}
{{- define "kube-resource-updater.annotations" -}}
{{- with .Values.commonAnnotations }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "kube-resource-updater.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "kube-resource-updater.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference.
*/}}
{{- define "kube-resource-updater.image" -}}
{{- $registry := .Values.global.imageRegistry | default .Values.image.registry -}}
{{- $repository := .Values.image.repository -}}
{{- $separator := ":" -}}
{{- $termination := .Values.image.tag | default .Chart.AppVersion | toString -}}
{{- if .Values.image.digest }}
{{- $separator = "@" -}}
{{- $termination = .Values.image.digest -}}
{{- end }}
{{- printf "%s/%s%s%s" $registry $repository $separator $termination -}}
{{- end }}

{{/*
Image pull secrets.
*/}}
{{- define "kube-resource-updater.imagePullSecrets" -}}
{{- $pullSecrets := list }}
{{- range .Values.global.imagePullSecrets -}}
{{- $pullSecrets = append $pullSecrets (dict "name" .) -}}
{{- end }}
{{- range .Values.image.pullSecrets -}}
{{- $pullSecrets = append $pullSecrets (dict "name" .) -}}
{{- end }}
{{- if $pullSecrets }}
imagePullSecrets:
{{ toYaml $pullSecrets }}
{{- end }}
{{- end }}

{{/*
Git token Secret name — provider-agnostic (Phase 3).

Resolution order (first non-empty wins):
  1. git.existingSecret      → operator-managed Secret, referenced by name.
  2. git.token               → chart-rendered Secret at templates/secret.yaml
                               named `<fullname>-git`.
  3. gitlab.existingSecret   → deprecated alias, operator-managed Secret.
  4. gitlab.token            → deprecated alias, chart-rendered Secret named
                               `<fullname>-gitlab` (legacy name preserved so
                               an in-place upgrade does not orphan the Secret).

Both empty → returns empty; the CronJob env gate skips GIT_TOKEN injection.
*/}}
{{- define "kube-resource-updater.gitSecretName" -}}
{{- if .Values.git.existingSecret -}}
{{- .Values.git.existingSecret -}}
{{- else if .Values.git.token -}}
{{- printf "%s-git" (include "kube-resource-updater.fullname" .) -}}
{{- else if .Values.gitlab.existingSecret -}}
{{- .Values.gitlab.existingSecret -}}
{{- else if .Values.gitlab.token -}}
{{- printf "%s-gitlab" (include "kube-resource-updater.fullname" .) -}}
{{- end }}
{{- end }}

{{/*
Git token Secret key — provider-agnostic.

Prefers git.existingSecretKey when the new stanza is active; falls back to
gitlab.existingSecretKey for the deprecated alias path. Defaults to "token"
which matches both the chart-rendered Secret and the conventional key name.
*/}}
{{- define "kube-resource-updater.gitSecretKey" -}}
{{- if or .Values.git.existingSecret .Values.git.token -}}
{{- .Values.git.existingSecretKey | default "token" -}}
{{- else -}}
{{- .Values.gitlab.existingSecretKey | default "token" -}}
{{- end }}
{{- end }}

{{/*
GitLab token Secret name — DEPRECATED alias. Kept so templates that were
already using this helper continue to render correctly. Delegates to the
provider-agnostic helper.
*/}}
{{- define "kube-resource-updater.gitlabSecretName" -}}
{{- include "kube-resource-updater.gitSecretName" . -}}
{{- end }}

{{/*
GitLab token Secret key — DEPRECATED alias. Delegates to the
provider-agnostic helper.
*/}}
{{- define "kube-resource-updater.gitlabSecretKey" -}}
{{- include "kube-resource-updater.gitSecretKey" . -}}
{{- end }}

{{/*
Pod security context.
*/}}
{{- define "kube-resource-updater.podSecurityContext" -}}
{{- if .Values.podSecurityContext.enabled }}
securityContext:
  fsGroup: {{ .Values.podSecurityContext.fsGroup }}
  runAsUser: {{ .Values.podSecurityContext.runAsUser }}
  runAsGroup: {{ .Values.podSecurityContext.runAsGroup }}
  runAsNonRoot: {{ .Values.podSecurityContext.runAsNonRoot }}
  {{- with .Values.podSecurityContext.seccompProfile }}
  seccompProfile:
    type: {{ .type }}
  {{- end }}
{{- end }}
{{- end }}

{{/*
Container security context.
*/}}
{{- define "kube-resource-updater.containerSecurityContext" -}}
{{- if .Values.containerSecurityContext.enabled }}
securityContext:
  runAsNonRoot: {{ .Values.containerSecurityContext.runAsNonRoot }}
  runAsUser: {{ .Values.containerSecurityContext.runAsUser }}
  allowPrivilegeEscalation: {{ .Values.containerSecurityContext.allowPrivilegeEscalation }}
  readOnlyRootFilesystem: {{ .Values.containerSecurityContext.readOnlyRootFilesystem }}
  capabilities:
    drop:
    {{- toYaml .Values.containerSecurityContext.capabilities.drop | nindent 6 }}
{{- end }}
{{- end }}

{{/*
Affinity rules — presets take effect unless .Values.affinity is explicitly set.
Uses common.affinities.pods from bitnami/common when preset is configured.
*/}}
{{- define "kube-resource-updater.affinity" -}}
{{- if .Values.affinity }}
affinity:
  {{- toYaml .Values.affinity | nindent 2 }}
{{- else if or .Values.podAffinityPreset .Values.podAntiAffinityPreset .Values.nodeAffinityPreset.type }}
affinity:
  {{- if .Values.podAffinityPreset }}
  podAffinity:
    {{- include "common.affinities.pods" (dict "type" .Values.podAffinityPreset "component" "" "customLabels" .Values.podLabels "context" $) | nindent 4 }}
  {{- end }}
  {{- if .Values.podAntiAffinityPreset }}
  podAntiAffinity:
    {{- include "common.affinities.pods" (dict "type" .Values.podAntiAffinityPreset "component" "" "customLabels" .Values.podLabels "context" $) | nindent 4 }}
  {{- end }}
  {{- if .Values.nodeAffinityPreset.type }}
  nodeAffinity:
    {{- include "common.affinities.nodes" (dict "type" .Values.nodeAffinityPreset.type "key" .Values.nodeAffinityPreset.key "values" .Values.nodeAffinityPreset.values) | nindent 4 }}
  {{- end }}
{{- end }}
{{- end }}

{{/* ------------------------------------------------------------------ */}}
{{/* Webhook helpers                                                       */}}
{{/* ------------------------------------------------------------------ */}}

{{/*
Webhook resource name suffix. Kept short to leave room for the release prefix
within the 63-char DNS label limit. Uses "-webhook" so adjacent helm releases
(e.g. kube-resource-updater main + future test releases) don't collide.
*/}}
{{- define "kube-resource-updater.webhook.fullname" -}}
{{- printf "%s-webhook" (include "kube-resource-updater.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Webhook component label. Used in selectors and labels so the webhook Deployment
and its Pods are independently selectable from the CronJob's resources.
*/}}
{{- define "kube-resource-updater.webhook.selectorLabels" -}}
{{ include "kube-resource-updater.selectorLabels" . }}
app.kubernetes.io/component: webhook
{{- end -}}

{{- define "kube-resource-updater.webhook.labels" -}}
{{ include "kube-resource-updater.labels" . }}
app.kubernetes.io/component: webhook
{{- end -}}

{{/*
NOTE: `webhook.certName` and `webhook.dnsNames` helpers were removed in
chart 1.20.0. Cert-manager was dropped in 1.1.0 — the in-process cert
reconciler (src/webhook_cert.py) generates the serving cert at startup
and dials the apiserver to patch the MWC/VWC caBundle. The Secret name
and DNS SANs are computed inside the Python code from `POD_NAMESPACE`
+ the webhook fullname helper, not from chart templates.
*/}}
