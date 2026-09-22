{{- define "llm-autotune.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "llm-autotune.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "llm-autotune.labels" -}}
app.kubernetes.io/name: {{ include "llm-autotune.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "llm-autotune.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "llm-autotune.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "llm-autotune.backendImage" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.image.backend $tag -}}
{{- end -}}

{{- define "llm-autotune.frontendImage" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.image.frontend $tag -}}
{{- end -}}

{{/*
Environment shared by the api, the worker and the migration job. Settings that
are secret come from the Secret; everything else from the ConfigMap. Both are
mounted wholesale so adding a setting is a one-line change here.
*/}}
{{- define "llm-autotune.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "llm-autotune.fullname" . }}-config
  - secretRef:
      name: {{ include "llm-autotune.fullname" . }}-secrets
{{- end -}}
