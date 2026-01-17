{{/*
Expand the name of the chart.
*/}}
{{- define "promtail.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "promtail.fullname" -}}
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
Create chart labels
*/}}
{{- define "promtail.labels" -}}
helm.sh/chart: {{ include "promtail.name" . }}
{{ include "promtail.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "promtail.selectorLabels" -}}
app.kubernetes.io/name: {{ include "promtail.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Loki service URL
*/}}
{{- define "promtail.lokiUrl" -}}
{{- if .Values.loki.serviceName }}
{{- printf "http://%s:%d/loki/api/v1/push" .Values.loki.serviceName (int .Values.loki.port) }}
{{- else }}
{{- printf "http://%s-loki:%d/loki/api/v1/push" .Release.Name (int .Values.loki.port) }}
{{- end }}
{{- end }}
