{{- define "ca.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ca.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "ca.labels" -}}
app.kubernetes.io/name: {{ include "ca.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "ca.selector" -}}
app.kubernetes.io/name: {{ include "ca.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: api
{{- end -}}

{{/* image reference: repository@digest when a digest is set, else repository:tag */}}
{{- define "ca.image" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}

{{- define "ca.collector" -}}
{{- printf "%s-otel-collector" (include "ca.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* collector configuration; called with (dict "exporters" <map>) */}}
{{- define "ca.collectorConfig" -}}
{{- $names := keys .exporters | sortAlpha | join ", " -}}
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
      grpc:
        endpoint: 0.0.0.0:4317
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20
  batch: {}
exporters:
  {{- toYaml .exporters | nindent 2 }}
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
service:
  extensions: [health_check]
  telemetry:
    metrics:
      level: none
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [{{ $names }}]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [{{ $names }}]
{{- end -}}
