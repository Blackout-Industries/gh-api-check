{{/*
Expand the name of the chart.
*/}}
{{- define "github-rate-limit-checker.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "github-rate-limit-checker.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "github-rate-limit-checker.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "github-rate-limit-checker.labels" -}}
helm.sh/chart: {{ include "github-rate-limit-checker.chart" . }}
{{ include "github-rate-limit-checker.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "github-rate-limit-checker.selectorLabels" -}}
app.kubernetes.io/name: {{ include "github-rate-limit-checker.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "github-rate-limit-checker.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "github-rate-limit-checker.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the GitHub token secret name
*/}}
{{- define "github-rate-limit-checker.tokenSecretName" -}}
{{- if .Values.github.token.existingSecret }}
{{- .Values.github.token.existingSecret }}
{{- else }}
{{- include "github-rate-limit-checker.fullname" . }}-token
{{- end }}
{{- end }}

{{/*
Get the GitHub App private key secret name
*/}}
{{- define "github-rate-limit-checker.appSecretName" -}}
{{- if .Values.github.app.privateKey.existingSecret }}
{{- .Values.github.app.privateKey.existingSecret }}
{{- else if .Values.github.app.privateKey.externalSecret.enabled }}
{{- .Values.github.app.privateKey.externalSecret.targetSecretName | default (printf "%s-app-credentials" (include "github-rate-limit-checker.fullname" .)) }}
{{- else }}
{{- include "github-rate-limit-checker.fullname" . }}-app-credentials
{{- end }}
{{- end }}

{{/*
Return the appropriate apiVersion for NetworkPolicy
*/}}
{{- define "github-rate-limit-checker.networkPolicy.apiVersion" -}}
{{- if semverCompare ">=1.7-0" .Capabilities.KubeVersion.GitVersion -}}
networking.k8s.io/v1
{{- else -}}
extensions/v1beta1
{{- end -}}
{{- end -}}

{{/*
Return the appropriate apiVersion for PodDisruptionBudget
*/}}
{{- define "github-rate-limit-checker.podDisruptionBudget.apiVersion" -}}
{{- if semverCompare ">=1.21-0" .Capabilities.KubeVersion.GitVersion -}}
policy/v1
{{- else -}}
policy/v1beta1
{{- end -}}
{{- end -}}

{{/*
Return the appropriate apiVersion for HorizontalPodAutoscaler
*/}}
{{- define "github-rate-limit-checker.hpa.apiVersion" -}}
{{- if semverCompare ">=1.23-0" .Capabilities.KubeVersion.GitVersion -}}
autoscaling/v2
{{- else -}}
autoscaling/v2beta2
{{- end -}}
{{- end -}}
