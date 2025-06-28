{{/*
Simple chart name.
*/}}
{{- define "technical-deck.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Simple full name: uses fullnameOverride if set, else "<release>-<chart>" or "<release>-<nameOverride>".
*/}}
{{- define "technical-deck.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (default .Chart.Name .Values.nameOverride) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "technical-deck.selectorLabels" -}}
app.kubernetes.io/name: {{ include "technical-deck.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name
*/}}
{{- define "technical-deck.serviceAccountName" -}}
{{- if .Values.serviceAccount.name }}
{{ .Values.serviceAccount.name }}
{{- else }}
{{ include "technical-deck.fullname" . }}
{{- end }}
{{- end }}


{{/*
Common labels
*/}}
{{- define "technical-deck.labels" -}}
app.kubernetes.io/name: {{ include "technical-deck.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
