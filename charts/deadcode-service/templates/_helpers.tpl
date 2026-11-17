{{/*
Trozos reutilizables. Se usan en las plantillas con:
  {{ include "deadcode.labels" (dict "root" . "component" "api") }}
Los archivos que empiezan con "_" no generan recursos.
*/}}

{{/*
Etiquetas estándar de Kubernetes (app.kubernetes.io/...): permiten
filtrar, por ejemplo: kubectl get pods -l app.kubernetes.io/component=api
*/}}
{{- define "deadcode.labels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Selector: el subconjunto FIJO de etiquetas con que un Deployment o
Service encuentra sus pods. No incluye la versión: si la incluyera,
cada actualización "perdería" a los pods anteriores.
*/}}
{{- define "deadcode.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Seguridad común de los pods de la app: usuario "app" (uid 1000).
*/}}
{{- define "deadcode.podSecurity" -}}
runAsUser: 1000
runAsGroup: 1000
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
Seguridad común de los contenedores: sin escalar privilegios, disco de
solo lectura, sin capacidades especiales de Linux.
*/}}
{{- define "deadcode.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{/*
Variables de conexión a Postgres. Las credenciales salen del Secret: la
contraseña nunca se escribe en ningún archivo. Va en PGPASSWORD (la lee
sola la librería de Postgres) y NO dentro de DATABASE_URL: la que genera
AWS para RDS puede traer símbolos como # ? % que en una URL significan
otra cosa ("#" la corta). Una sola vez para la API y el worker.
*/}}
{{- define "deadcode.databaseEnv" -}}
{{- $secret := .Values.postgres.existingSecret -}}
- name: POSTGRES_USER
  valueFrom:
    secretKeyRef: {name: {{ $secret }}, key: POSTGRES_USER}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef: {name: {{ $secret }}, key: POSTGRES_PASSWORD}
- name: POSTGRES_DB
  valueFrom:
    secretKeyRef: {name: {{ $secret }}, key: POSTGRES_DB}
- name: DATABASE_URL
  value: postgresql://$(POSTGRES_USER)@{{ .Values.postgres.host }}:5432/$(POSTGRES_DB)?sslmode={{ .Values.postgres.sslMode }}
{{- end }}

{{/* Imagen completa de la app: repositorio:etiqueta */}}
{{- define "deadcode.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end }}
