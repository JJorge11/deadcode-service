# deadcode-service

Servicio que analiza repositorios públicos y detecta código muerto
combinando análisis estático y cobertura en ejecución.

## Objetivo
Proyecto de aprendizaje y portafolio (DevOps, contenedores, bases de
datos, nube, IA). Lo importante es aprender, no terminar rápido.

## Arquitectura
API (FastAPI) -> cola (Redis) -> workers -> PostgreSQL -> dashboard.
Los análisis corren dentro de contenedores aislados, sin acceso a secretos.

## Analizadores (módulos intercambiables, misma interfaz)
1. Python: Vulture (estático) + coverage.py (ejecución)
2. JS/TS: Knip
3. Ruby (opcional): Coverband, limitado por falta de tráfico real

## Etapas
1. Docker Compose (API, worker, Postgres, Redis)
2. Tests y CI con GitHub Actions
3. Imágenes en GHCR
4. Kubernetes local (kind, Helm)
5. GitOps con ArgoCD
6. Observabilidad (Prometheus, Grafana)
7. Nube con Terraform (AWS, presupuesto controlado)
8. Capa de IA con evaluaciones medibles

## Estado actual
<!-- Actualizar al final de cada sesión. Es el "punto de guardado":
     refleja lo que es verdad AHORA. El historial vive en git. -->
- Etapa 1 COMPLETA: Docker Compose con API + worker + Postgres 17 +
  Redis 8, con healthchecks. Docker Desktop (WSL2) instalado.
- Flujo funcionando: POST /analyses crea la fila en Postgres (queued)
  y encola; el worker la pasa a running -> finished/failed y guarda el
  resultado. GET /analyses y GET /analyses/{id} leen de Postgres.
  Probado: los datos sobreviven a `docker compose down` y a vaciar Redis.
- Análisis REAL con Vulture funcionando (ya no es simulado).
- /health revisa Postgres y Redis (200 / 503).
- Etapa 2 en curso: 15 tests con pytest contra Postgres y Redis reales
  (base "deadcode_test" y Redis db 1). En Windows: 14 pasan + 1 saltado
  (worker real, solo Linux). En Linux: 15 pasan.
- CI funcionando en GitHub Actions (.github/workflows/ci.yml): tests +
  build de la imagen + validación de Compose. Primer run verde (~1 min).
- Etapa 3 COMPLETA: el CI publica la imagen en GHCR
  (ghcr.io/jjorge11/deadcode-service) con etiquetas sha-<commit> y
  latest. Paquete público: se descarga sin login. Verificado: la imagen
  descargada corre y /health responde ok.
- Protección SSRF lista: POST /analyses solo acepta
  https://github.com/<dueño>/<repo> (validation.py) y guarda la URL
  normalizada. 52 tests pasan en Windows (38 de validación) + 1 saltado.
- Análisis real con Vulture, en 3 partes:
  1. Explorar Vulture (HECHO, ver "Aprendizajes de Vulture").
  2. HECHO: el worker descarga el .tar.gz de GitHub y analiza con
     Vulture + reglas por framework. 73 tests pasan (+1 saltado).
     Probado con repos reales: este repo (0 hallazgos), psf/requests
     (88), pallets/itsdangerous (21), repo inexistente (error claro).
  2b. HECHO: clasificación propia (python_classify.py): location,
     category, visibility, priority (high/medium/low) y reason. 101
     tests pasan (+1 saltado). Flask: de 106 hallazgos -> 1 high
     (_path_is_ancestor, muerto real), 15 medium, 90 low.
  3. HECHO: cada análisis en su propio contenedor desechable
     (sandbox.py). 112 tests pasan (+1 saltado). Verificado en vivo:
     sandbox sin red, sin variables secretas, solo lectura, nobody,
     cap-drop ALL, límites; 0 sandboxes vivos al terminar; el portero
     bloquea imágenes/volúmenes/info (403) y la API no lo ve.
- Etapa 4 (Kubernetes local) COMPLETA. Pasos:
  1. HECHO: kind v0.33, Helm v4.3, kubectl v1.36 (de Docker Desktop).
     Clúster "deadcode" (k8s v1.37) con k8s/kind-config.yaml: 1
     control-plane + 1 worker; puerto 30080 del clúster -> 127.0.0.1:8080.
     Demo hecha: Deployment de 2 réplicas desde GHCR y auto-curación
     al borrar un pod (demo borrada después).
  2. HECHO: Postgres (StatefulSet + PVC 1Gi) y Redis (Deployment, sin
     disco) en el namespace "deadcode", con manifiestos propios (sin
     Bitnami; hoy dentro del chart). Secret "postgres-credentials" creado por
     comando (no en git). Verificado: usuarios correctos, PONG,
     SELECT version(), y el dato sobrevive a borrar el pod postgres-0.
  3. HECHO: API (2 réplicas, Service NodePort 30080 -> 127.0.0.1:8080)
     y worker con la imagen ghcr.io/jjorge11/deadcode-service:sha-4990c1d.
     ConfigMap "deadcode-config" (REDIS_URL, SANDBOX_MODE). Verificado:
     /health 200, análisis real de punta a punta, y 60/60 peticiones OK
     borrando un pod de la API en medio.
     (Entonces el worker usaba SANDBOX_MODE=local, sin
     aislamiento; reemplazado por "kubernetes" en el paso 5.)
  4. HECHO: chart de Helm en charts/deadcode-service (k8s/base/
     eliminado: una sola fuente de verdad). Release "deadcode" en el
     namespace "deadcode". Migración: los selectores son INMUTABLES, así
     que se borraron Deployments/StatefulSet/Services/ConfigMap creados
     con kubectl (NO el PVC, el Secret ni el namespace) y Helm los
     recreó con los mismos nombres: Postgres reutilizó su disco (datos
     conservados, verificado). Probado upgrade (--set api.replicas=3) y
     rollback. CI: nuevo job "helm" (lint --strict + template) y publish
     ahora depende también de él.
  5. Sandbox en Kubernetes (un POD desechable por análisis), en partes:
     5.1 HECHO: templates/sandbox.yaml -> namespace "deadcode-sandboxes"
         con Pod Security "restricted", NetworkPolicy deny-all,
         ResourceQuota (4 pods, 2Gi, 4 CPU), ServiceAccount "sandbox"
         sin token, ServiceAccount "worker" + Role/RoleBinding (solo
         pods, pods/attach y pods/log en ese namespace). API, Postgres
         y Redis con automountServiceAccountToken: false.
         Verificado: kindnet SÍ aplica NetworkPolicy (no hace falta
         Calico); can-i del worker (sí pods en sandboxes; no secrets,
         no otros namespaces, no netpol); pod privilegiado rechazado
         (7 violaciones) y pod sin límites rechazado por la cuota;
         prueba manual con `kubectl run -i -q --overrides` pasando el
         .tar.gz por stdin -> JSON correcto en ~4 s.
     5.2 HECHO: SANDBOX_MODE "kubernetes" en sandbox.py (kubectl run
         -i -q --overrides, pod borrado SIEMPRE en finally), kubectl
         v1.36.1 en la imagen (registry.k8s.io/kubectl), ConfigMap con
         SANDBOX_IMAGE y SANDBOX_NAMESPACE. Modo desconocido = error
         (antes caía en docker). 123 tests pasan (+1 saltado); mutaciones
         (no borrar pod, 137 = memoria) detectadas. Experimento: kubectl
         devuelve 137 TANTO por OOMKilled COMO por DeadlineExceeded ->
         se consulta el motivo con "kubectl get pod". Sin
         terminationGracePeriodSeconds: 0 el plazo tardaba 30 s más.
     5.3 EN CURSO: imagen sha-0eedf3e publicada (CI verde), helm
         revisión 5 con mode=kubernetes (values.yaml SIN commit aún).
         Análisis real de pallets/itsdangerous OK (21 hallazgos, ~5 s,
         pod creado y borrado). Espía dentro del sandbox: nobody, sin
         token, disco solo lectura OK; PERO ⚠️ CARRERA DE RED: durante
         ~1 s tras nacer el pod, kindnet aún no aplica la NetworkPolicy
         (conectó a Postgres, API de K8s e internet; desde t=1,5 s todo
         bloqueado). Riesgo HOY bajo (Vulture no ejecuta el código del
         repo, solo lo lee), ALTO para coverage.py.
         ARREGLADO (sin commit aún): initContainer "esperar-sin-red"
         (wait_no_network.py): intenta conectar a la API de K8s y a
         1.1.1.1; solo termina OK si NADIE responde ("rechazada" cuenta
         como red abierta); tras 20 intentos falla y el análisis NO
         corre (motivo NetworkNotBlocked en logs; usuario ve "Error
         inesperado"). 135 tests pasan (+1 saltado). Probado en el
         clúster con imagen local (kind load, tag "portero", helm
         revisiones 6-7): espía ya no llega a nada; itsdangerous OK en
         ~6,5 s (+2 s); borrando la NetworkPolicy a propósito el
         análisis falla cerrado en ~14 s; helm upgrade la recreó.
         HECHO: commit 869245e -> CI verde -> image.tag sha-869245e ->
         helm revisión 8 (--reset-values, sin la imagen local).
         itsdangerous OK (21 hallazgos, ~7 s), 0 sandboxes vivos.
- Etapa 4 COMPLETA. El modo "local" ya NO se usa en el clúster.
  Actualizar la versión exige 2 commits (código -> CI publica sha ->
  commit con el nuevo image.tag): la etapa 5 (ArgoCD) lo automatiza.
- HECHO: límite de procesos por pod (podPidsLimit: 1024 en el kubelet de
  AMBOS nodos; antes era ilimitado). En k8s/kind-config.yaml
  (kubeadmConfigPatches) para clústeres nuevos; en el clúster existente
  aplicado EN CALIENTE (ver "Comandos útiles"), sin recrearlo y sin
  reiniciar pods. 1024 = ~7x el pod más grande (Grafana, ~150 PIDs).
  Verificado: fork bomb de prueba en deadcode-sandboxes frenó en 1022
  hijos (+ padre + pause = 1024) sin afectar a otros pods; análisis
  real OK (21 hallazgos, 9,4 s). Límite por NODO (K8s no permite uno
  por pod): lo ideal es un nodo solo para sandboxes con límite menor
  (~128, como en Docker) -> pendiente para la nube (node pool aparte).
- Etapa 5 (GitOps con ArgoCD) COMPLETA. Pasos:
  1. HECHO: ArgoCD v3.6.1 instalado con el manifiesto oficial
     (kubectl apply --server-side) en el namespace "argocd" (7 pods).
     Web por port-forward (https://127.0.0.1:8443), usuario admin,
     contraseña inicial en el Secret argocd-initial-admin-secret
     (NO va en git ni se muestra en el chat).
  2. HECHO: Application "deadcode" (argocd/deadcode.yaml, aplicada una
     vez con kubectl) vigila charts/deadcode-service en main, con
     automated selfHeal + prune, SIN finalizer (borrar la Application
     no borra la app). Primero sin automático: los 15 recursos ya
     estaban Synced (cero sorpresas). Sync manual -> anotación
     argocd.argoproj.io/tracking-id en cada recurso; sin reinicios.
     Helm "olvidó" el release borrando SOLO sus Secrets
     (owner=helm,name=deadcode; revisiones 1-8): pods, PVC, Secret de
     Postgres y los 7 análisis intactos. Desde aquí NO usar helm
     upgrade/rollback en el clúster: todo cambio es un commit.
  3. HECHO: demostraciones. selfHeal: NetworkPolicy borrada a mano,
     recreada en 3 s. Cambio solo con git: commit 10c9bbb (API a 3
     réplicas) aplicado sin tocar el clúster, pero tardó ~6,5 min
     (ArgoCD pregunta a GitHub cada ~3 min; un webhook no sirve porque
     ArgoCD no es accesible desde internet). Para no esperar: botón
     Refresh o kubectl annotate application deadcode -n argocd
     argocd.argoproj.io/refresh=normal --overwrite. Volver atrás:
     git revert (ce78a98) + refresh -> 2 réplicas en 4 s.
  4. HECHO: job "deploy" en ci.yml (needs: publish; solo push a
     main; único con contents: write; concurrency "deploy"): escribe
     tag: sha-<7 chars> en values.yaml con sed (falla si no encuentra
     la línea), commit como github-actions[bot], pull --rebase y push.
     Sin bucle: los commits hechos con GITHUB_TOKEN no disparan
     workflows. ⚠️ El robot hace commits en main: SIEMPRE `git pull`
     antes de trabajar. Todo push a main redespliega (aunque solo
     cambie un .md): aceptable por ahora. Verificado: push 0f7478e ->
     5 jobs verdes -> commit del bot 0eb4a58 (tag sha-0f7478e) -> 0 CI
     lanzados por él -> ArgoCD Synced/Healthy -> análisis real OK.
- Flujo de despliegue ACTUAL: git push -> CI (tests, build, helm) ->
  publish (imagen sha-X) -> deploy (bot: tag sha-X) -> ArgoCD -> clúster.
  Nadie ejecuta helm ni kubectl para desplegar.
- Etapa 6 (Observabilidad) COMPLETA (los 5 pasos verificados en vivo;
  destino de notificaciones de Alertmanager decidido NO configurarlo por
  ahora: las alertas se ven en su web y en Grafana; retomar en la nube).
  Plan original: 1) Prometheus + Grafana con
  ArgoCD; 2) métricas propias en la API; 3) métricas de análisis (cola,
  duración, fallos por motivo); 4) dashboard propio en git; 5) 1-2
  alertas (sandbox huérfano, disco de Postgres).
  1. HECHO (commit cca5546): Application "monitoring" (argocd/monitoring.yaml)
     con el chart oficial kube-prometheus-stack 93.3.0 (versión fija),
     namespace "monitoring", Secret "grafana-admin" creado por comando.
     Apagados etcd/scheduler/controller-manager/kube-proxy (en kind
     escuchan en 127.0.0.1: falsas alarmas) y Alertmanager (paso 5).
     Prometheus: PVC 2Gi, 7 días. Verificado: 16/16 targets up, 24
     dashboards, login OK y "prom-operator" rechazada.
     ⚠️ INCIDENTE al instalar: Grafana 13 con límite 256Mi -> casi sin
     memoria, Linux releía su programa del disco sin parar (18 GB
     leídos) -> disco de WSL2 saturado -> etcd lento -> API de K8s caída
     (apiserver/scheduler/controller-manager reiniciados) -> un análisis
     falló (kubectl run: timeout) y otro tardó 84 s. Arreglo: límite
     768Mi (usa ~580), base de Grafana en RAM (inMemory) y startupProbe
     de 5 min. Lección: MEDIR la memoria real antes de fijar límites.
     Tras arreglarlo: análisis OK (21 hallazgos) en ~21 s mientras el
     disco se recuperaba; tras el deploy de cca5546, ~7,8 s (normal).
  - BUG encontrado: sandbox._delete_pod no revisaba el código de salida
    de "kubectl delete" (subprocess.run NO lanza error si el comando
    falla): con la API caída, el pod quedó huérfano SIN log (pasó con
    sandbox-67cfe200cec1; borrado a mano). ARREGLADO (cca5546):
    revisa returncode, 3 intentos con 2 s de pausa y, si todos fallan,
    log de aviso con el motivo; nunca lanza error. 137 tests pasan
    (+1 saltado); mutante "ignorar el código de salida" detectado.
    Red de seguridad pendiente: alerta de sandbox huérfano (paso 5).
  2. HECHO (commit cfcf98a, verificado en el clúster: 18/18 targets,
     los 2 pods de la API medidos, /health p95 = 0,044 s): métricas de la API
     con prometheus_client (metrics.py): Counter
     deadcode_http_requests_total{method,route,status} y Histogram
     deadcode_http_request_duration_seconds{method,route}, medidos en
     un middleware; GET /metrics oculto en /docs y no se mide a sí
     mismo. route = PLANTILLA (/analyses/{analysis_id}), nunca la URL
     real; rutas inexistentes = "sin_ruta" (evita "explosión de
     cardinalidad": una serie por cada id o URL de robot). Un solo
     proceso uvicorn: no hace falta el modo multiproceso. Chart: puerto
     del Service con nombre "http" + templates/servicemonitor.yaml
     (metrics.serviceMonitor.enabled/interval 15s). 144 tests pasan
     (+1 saltado); mutante "URL real como etiqueta" detectado.
  3. HECHO (commit 70e6fa5, verificado en el clúster: source_up=1 en
     los 2 pods, fallos repo_no_existe/inesperado, 11 de 15 análisis en
     <10 s; los lentos y los "inesperado" son del incidente): métricas de los
     análisis (analysis_metrics.py). NO se cuentan en el worker: RQ hace
     fork y lo que cuenta la copia se pierde. Se leen de la fuente de
     verdad (Postgres + Redis) en cada visita a /metrics de la API
     (custom Collector): deadcode_queue_jobs, deadcode_analyses{status},
     deadcode_analyses_failed_total{reason} (mensaje -> motivo FIJO por
     fragmento; desconocido = "otro"), deadcode_analysis_duration_seconds
     (histograma de finished_at - created_at, solo terminados bien:
     incluye la espera en cola), deadcode_analyses_oldest_pending_seconds
     (worker atascado) y deadcode_metrics_source_up{source} (si Postgres
     o Redis caen, /metrics responde igual). ⚠️ Las 2 réplicas de la API
     reportan lo MISMO: en Grafana usar max(), no sum(). describe()
     vacío para no consultar Postgres al importar. Sandboxes vivos: ya
     los mide kube-state-metrics. 156 tests pasan (+1 saltado); mutantes
     "duración incluye fallidos" y "todo es otro" detectados.
  4. HECHO (commit 313d508; verificado: cargado en Grafana, 13 paneles,
     provisioned=True -> no se puede guardar desde la web): dashboard
     "deadcode-service" (uid deadcode-overview), 13 paneles en 3 filas:
     API (peticiones/s sin /health, % 5xx, p95), análisis (cola,
     pendiente más viejo, estados, fallos por motivo, por hora, p50/p95)
     y sandboxes/salud (sandboxes vivos con kube_pod_info, source_up,
     reinicios, memoria). FUENTE: scripts/make_dashboard.py (Python con
     comentarios) -> --write genera charts/deadcode-service/dashboards/
     deadcode.json -> templates/dashboard.yaml lo mete en un ConfigMap con
     grafana_dashboard: "1" -> el sidecar de Grafana lo carga (busca en
     TODOS los namespaces). editable: false: git manda. Cada consulta se
     probó antes contra el Prometheus real. tests/test_dashboard.py: JSON
     al día con el script, ids únicos y cada métrica deadcode_* existe
     (nombre EXACTO: la 1a versión usaba "contiene" y dejaba pasar
     queue_job). 159 tests (+1 saltado).
     ⚠️ Helm procesa TODO el archivo de plantilla, también los
     comentarios: escribir llaves dobles en un comentario rompe el chart.
  5. HECHO (commit 7f9e6f2 + kubectl apply de monitoring.yaml).
     Verificado en vivo: promtool en el CI (5 reglas, SUCCESS); las 5
     reglas cargadas (tardan ~1 min tras el sync); Prometheus conectado
     a Alertmanager (se apagó PrometheusNotConnectedToAlertmanagers);
     Alertmanager usa ~23 MiB (límite 128Mi, holgado); base = 7,5 MiB
     (0,74% del disco). PRUEBA REAL: pod falso en deadcode-sandboxes
     (cumpliendo restricted) -> pending a los 5,3 min -> firing y
     recibida por Alertmanager a los 6,3 min (texto correcto: "lleva 6m
     7s vivo") -> borrado el pod, resuelta en <1 min. Única alerta
     activa: Watchdog (siempre encendida A PROPÓSITO: si deja de llegar,
     el monitoreo mismo está roto). Las tildes se ven como "Ã©" en
     PowerShell/Git Bash, pero los datos están bien (verificado byte a
     byte). Pendiente opcional: destino de notificaciones.
     Detalle:
     alertas. Reglas en charts/deadcode-service/alerts/deadcode.rules.yaml
     (formato Prometheus) -> templates/alerts.yaml las mete en un
     PrometheusRule (metrics.alerts.enabled). 5 reglas: DeadcodeAPICaida
     (absent(up==1), 2m, critical), DeadcodeFuenteDeMetricasCaida
     (source_up==0, 2m, critical), DeadcodeWorkerAtascado (pendiente >
     600 s, 2m), DeadcodeSandboxHuerfano (time()-kube_pod_created > 600 desde el nivel 2; antes 300,
     1m) y DeadcodePostgresCasiLleno (> 80%, 10m). En kind el disco
     local-path NO da kubelet_volume_stats: nueva métrica
     deadcode_postgres_database_bytes (pg_database_size, sin el WAL:
     aproximada) / kube_persistentvolumeclaim_resource_requests_storage_bytes.
     Tests con promtool (tests/alerts/deadcode.rules.test.yaml): 6 casos
     con series simuladas (incluye "solo 1 réplica cae = sin alerta" y
     "sandbox normal no alerta"); en CI dentro del job "helm" con
     quay.io/prometheus/prometheus:v3.16.0. Aprendido: absent() con
     filtro (== 1) NO copia las etiquetas. Mutante "umbral 3000"
     detectado. Alertmanager activado en monitoring.yaml (64/128Mi:
     MEDIR); aún sin destino de notificaciones (se ven en su web y en
     Grafana). 160 tests (+1 saltado).
- Reinicio del PC: Docker Desktop NO arranca solo. Abrirlo -> el
  clúster kind vuelve solo (~1-2 min, verificado) -> Compose NO
  (docker compose up -d). Los datos de Postgres sobreviven.
- Nivel 2 (coverage.py) EN CURSO. Paso 1 (explorar a mano) HECHO: ver
  "Aprendizajes de coverage.py". Paso 2 (dependencias, opción A: pod de
  descarga con internet pero sin red interna), en partes:
  2.1 HECHO: dependencies.py (pyproject.toml -> lista de requisitos,
      validados con "packaging", ahora dependencia de la app; RECHAZA
      URLs directas "pkg @ ..."; coverage fija ==7.17.3; máx. 100) y
      downloader_main.py (stdin .tar.gz -> stdout .tar con
      resultado.json PRIMERO + requirements.txt + wheels/; solo lee el
      pyproject.toml de la RAÍZ sin escribir a disco; pip del sistema
      /usr/local/bin/python3 con --isolated --only-binary=:all:
      --index-url PyPI; máx. 150 MB, 180 s). 32 tests nuevos (pip
      simulado), 193 pasan (+1 saltado); mutantes detectados (URL,
      ciclos, coverage del repo, subcarpeta, tamaño, --isolated).
      Prueba real en contenedor nobody/solo lectura CON red:
      itsdangerous 10 wheels 2,5 MB 6 s; requests 33 / 11 MB / 16 s;
      este repo 39 / 18 MB / 21 s; URL directa y paquete inexistente
      -> error claro.
  2.2 HECHO: NetworkPolicy "downloader-internet" en templates/sandbox.yaml
      (se SUMA a deny-all): solo pods con deadcode.role=downloader;
      salida a DNS del clúster (kube-dns, 53) y TCP 443 a 0.0.0.0/0
      EXCEPTO 10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10, 127/8.
      Portero: wait_no_network --solo-red-interna (solo exige que la API
      de K8s no responda; sin destinos que probar = falla cerrado, porque
      all([]) es True). 197 tests (+1 saltado); mutante "sin destinos"
      detectado. Verificado en vivo (netpol aplicada a mano antes del
      commit; ArgoCD la adopta): pod downloader -> PyPI conecta;
      1.1.1.1:80, API de K8s, ArgoCD (¡también 443!), Postgres, Redis,
      kubelet, 169.254.169.254 y host de Docker bloqueados; pod SIN
      etiqueta: ni DNS. Pod completo (portero + downloader_main, imagen
      local "portero2" con kind load): requests 33 wheels / 11 MB en 22 s;
      repo con URL directa rechazado; 0 pods vivos al terminar.
  2.3 HECHO: sandbox.download_dependencies(tarball) -> .tar (o
      DependencyError con motivo claro). Lo común de los 2 pods quedó en
      _pod_spec() y _run_pod() (crear, traducir fallos, borrar SIEMPRE).
      Etiqueta deadcode.role: "analisis" / "downloader" (la que abre
      internet; test + mutante "análisis con etiqueta de internet").
      Pod de descarga: 240 s, 768Mi, /tmp 256Mi, prefijo "descarga-".
      Modo docker: NO disponible a propósito (en Compose no se puede dar
      internet sin la red de tu PC, donde están Postgres y Redis).
      Respuesta rota del pod = SandboxError (no es culpa del repo).
      212 tests (+1 saltado); 5 mutantes detectados. En vivo (sandbox.py
      desde el PC con SANDBOX_MODE=kubernetes e imagen local portero2):
      análisis Vulture OK (21, 8 s); itsdangerous 10 wheels 20 s;
      requests 33 wheels 39 s; URL directa y paquete inexistente -> error
      claro; 0 pods vivos. Aún NO se llama desde tasks.py (paso 5).
  Paso 3 (pod de TESTS: el "cocinero", sin red), en partes:
  3.1 HECHO: coverage_main.py. Entrada: .tar con repo.tar.gz + deps.tar
      (pack_input, lo usará el worker). Extrae TODO el repo
      (fetcher.extract_all_files, mismos límites) y de deps solo
      requirements.txt + wheels/*.whl. venv en /tmp con el Python del
      sistema -> pip --no-index -> repo --editable (si falla: sigue con
      PYTHONPATH) -> coverage run con NUESTRO --rcfile (ignora la config
      del repo) -m pytest --continue-on-collection-errors -> coverage json
      -> {percent, files: {ruta: {executed, missing}}} (rutas resueltas
      contra el repo; fuera = descartadas). Código ajeno: salida a logs
      (no a nuestro stdout), entorno mínimo, plazo 180 s con SIGINT
      (coverage guarda) y luego SIGKILL al grupo. pytest con código
      distinto de 0/1 y 0% = error claro ("no pudieron ejecutarse: <motivo>").
      Prueba real en Docker SIN red (nobody, solo lectura, 1g):
      itsdangerous 297 OK 97,2% 16 s; click 86,8% 46 s; requests 87,9%
      112 s; este repo -> "no pudieron ejecutarse: KeyError:
      'POSTGRES_USER'". Repo tramposo: JSON falso impreso NO llegó a
      nuestra salida; sleep infinito cortado a los 8 s CON datos; salir a
      internet falló. 27 tests (3 solo Linux: señales; probados en
      contenedor), 236 pasan (+4 saltados); 6 mutantes detectados.
      Encontrados por los tests: AttributeError sin atrapar en informe
      roto; rutas "/x" y "../" se colaban.
      Límite conocido: el código ajeno (mismo usuario) podría falsear el
      informe de SU repo; no puede salir del pod.
  3.2 HECHO: sandbox.run_coverage(tarball, deps) -> {tests, coverage} o
      CoverageError. Pod "tests-..." con etiqueta deadcode.role=tests
      (SIN red: portero completo), 360 s, 1Gi, /tmp en DISCO (emptyDir
      sin medium, 2Gi; repo + wheels + entorno). Docker: no disponible.
      Alerta DeadcodeSandboxHuerfano subida a > 600 s (test promtool:
      pod de tests de 6 min NO alerta; mutante "300" detectado). Cuota
      2Gi alcanza: los pods de un análisis van uno tras otro.
      245 tests (+4 saltados); mutantes "tests con gafete/portero de
      descarga" y "/tmp en memoria" detectados. EN VIVO (imagen local
      nivel2 con kind load, descarga -> tests): itsdangerous 13 s + 26 s,
      97,2%; click 10 + 44 s, 86,8%; requests 37 + 151 s, 87,9% (tests
      123 s: cerca del límite de 180); este repo -> "no pudieron
      ejecutarse: KeyError: 'POSTGRES_USER'"; tramposo: plazo REAL de
      180 s -> Ctrl+C -> datos guardados (usada sí, nunca no), internet
      falló; 0 pods vivos al terminar.
      ⚠️ Para el paso 5: RQ corta cada trabajo a los 180 s POR DEFECTO
      (no hay job_timeout): el nivel 2 completo puede tardar ~10 min ->
      fijar job_timeout al encolar.
      Commit 2f107a8 (2.2 a 3.2 juntos): CI verde, ArgoCD adoptó la
      NetworkPolicy aplicada a mano (tracking-id), alerta en 600 s,
      análisis nivel 1 OK (21, 8,3 s).
  Paso 4 HECHO (sin commit): analyzers/python_cross.py, PURO (solo
  compara números; el worker no lee código ajeno). Usa "last_line" que
  ya entregaba Vulture. Cuerpo = line+1 .. last_line; solo function,
  method y property (import/variable/attribute/class: None). executed
  True -> priority low + FALSE_POSITIVE_REASON; False -> medium sube a
  high, high/low igual, motivo + ". Además, los tests del repo nunca lo
  ejecutaron."; None -> igual. Guarda "static_priority" (lo que dijo
  Vulture) y reordena (sort_findings, sacada de python_classify).
  summarize_cross -> {confirmed, false_positives, not_applicable}.
  Con datos REALES (Vulture en contenedor + coverage de la 3.1):
  itsdangerous 0/0/21; click 6 confirmados, 5 falsos positivos
  (_wrap_chunks high -> low); requests 15 / 7 (MockRequest.get_full_url
  & co. y propiedades -> low; _find high confirmado); high de requests:
  5 -> 19. 21 tests, 266 pasan (+4 saltados); 5 mutantes detectados.
  Paso 5 HECHO (sin commit): POST /analyses acepta "dynamic": true
  (Field strict: solo true/false; responde "dynamic"). Se encola
  analyze_repo(id, url, dynamic) con job_timeout = LEVEL1 300 s /
  LEVEL2 1200 s (antes NO había: RQ cortaba a los 180 s). dynamic con
  valor por defecto False: los trabajos viejos (2 argumentos) siguen.
  tasks._add_dynamic: Vulture -> descarga -> tests -> cruce; resumen
  recalculado; result["dynamic"] = {status finished, coverage_percent,
  tests {summary, exit_code, timed_out}, cross} o {status failed,
  error}. Si el nivel 2 falla (esperado O bug), el análisis queda
  "finished" con lo de Vulture: el nivel 2 nunca hace perder el 1.
  Sin cambio de tabla (el nivel viaja en el trabajo y en el resultado).
  Alerta DeadcodeWorkerAtascado subida a > 1500 s (cuenta el análisis
  EN CURSO, y un nivel 2 puede durar 20 min); test promtool con "20 min
  = normal". 277 tests (+4 saltados); 5 mutantes detectados. EN VIVO
  (tasks._run_analysis desde el PC, imagen nivel2): click 67 s (6
  confirmados, 5 falsos positivos); itsdangerous 47 s; este repo: nivel
  2 falla con motivo claro y el nivel 1 llega (7 hallazgos) pero tardó
  258 s (en la 3.2 ~78 s): SIN EXPLICAR, revisar. 0 pods vivos.
  Commit 2626c86: CI ROJO (test_worker.py, solo Linux, simulaba
  _run_analysis con la firma vieja) -> arreglo 37dd351 (suite completa
  en Linux ANTES del push: 281, 0 saltados) -> CI verde -> desplegado.
  PRUEBA POR LA API REAL (127.0.0.1:8080): "dynamic": "si" -> 422;
  click nivel 2 -> finished, 6 confirmados / 5 falsos positivos, 86,8%
  (~142 s); itsdangerous sin dynamic -> nivel 1 igual que siempre, sin
  sección "dynamic"; este repo nivel 2 -> finished con los 7 hallazgos
  de Vulture + dynamic {status failed, "no pudieron ejecutarse:
  KeyError: 'POSTGRES_USER'"} (~172 s). Línea de tiempo de pods: cada
  pod pasa ~15-20 s en Pending (arranque + portero) -> un nivel 2 paga
  ~50 s solo en arrancar 3 pods; el pod de tests de este repo, 82 s
  (instalar 39 wheels). Los 258 s de antes no se repitieron (la prueba
  desde el PC compartía el clúster con otras cosas); vigilar en Grafana.
  Paso 6 HECHO (sin commit): métricas del nivel 2 en analysis_metrics.py
  (leídas de Postgres, como el resto): deadcode_level2_analyses_total
  {status finished/failed}, deadcode_level2_failed_total{reason} (motivos
  fijos LEVEL2_FAILURE_REASONS, en ORDEN: "tiempo máximo" gana a
  "instalación"; un fallo del nivel 2 NO cuenta en
  deadcode_analyses_failed, el análisis queda "finished") y
  deadcode_level2_findings_total{verdict confirmado/falso_positivo/
  no_aplica}. deadcode_analysis_duration_seconds ahora con etiqueta
  level ("2" = el resultado trae "dynamic") y cubetas hasta 1200 s.
  Dashboard: fila "Nivel 2" (4 paneles: % que funcionó, % falsos
  positivos de Vulture, fallos por motivo, veredictos) y duración por
  nivel (max by (le, level)). test_dashboard: la regex no aceptaba
  dígitos (deadcode_level2 -> "deadcode_level"): arreglada. SQL probado
  contra el Postgres REAL del clúster: nivel 1 = 18, nivel 2 = 2 (144 s
  en <=300, 339 s en <=600); cruce 6/5/126 y 1 fallo KeyError. 298
  tests en Windows (+4 saltados), 302 en Linux; 3 mutantes detectados
  (todo nivel 1, motivo olvidado, veredictos cruzados).
  SIGUIENTE: commit -> verificar en Grafana; luego el dashboard web.
- Pendiente después: dashboard, README.
- Pendiente: dashboard simple (el usuario tuvo dificultad usando /docs
  para consultar resultados: copiar ids y leer JSON es incómodo).
- Pendiente: README (se dejó para más adelante); migraciones con
  Alembic cuando cambie la tabla.

## Decisiones
<!-- Una línea por decisión: qué se eligió y por qué. -->
- uv para dependencias: rápido, con lockfile (uv.lock) para instalaciones
  reproducibles. uv.lock se versiona en git.
- Layout `src/`: obliga a instalar el paquete para importarlo y evita
  errores que solo aparecen al empaquetar o dentro de Docker.
- FastAPI para la API: tipado, validación automática y docs en /docs.
- Imagen base python:3.12-slim + uv copiado de su imagen oficial (misma
  versión que en local). Dependencias antes que el código para
  aprovechar la caché de Docker. El contenedor corre como usuario
  sin privilegios (`app`), no como root.
- Credenciales en .env (ignorado por git); .env.example documenta las
  variables. Versiones de imágenes fijas (postgres:17, redis:8).
- Postgres y Redis publicados solo en 127.0.0.1 (no visibles en la red).
- Postgres en contenedor solo para desarrollo; en la nube se usará un
  servicio administrado (RDS).
- Clientes: psycopg 3 (Postgres) y redis-py. Conexión nueva por cada
  /health por simplicidad; más adelante, pool de conexiones.
- /health: timeout de 2 s por servicio; al cliente solo se le muestra
  el tipo de error, el detalle va a los logs.
- Cola con RQ (no Celery): más simple y suficiente para aprender.
  Cola "analyses". El worker usa la misma imagen que la API con otro
  comando. RQ usa fork: el worker solo corre en Linux (contenedor).
- Finales de línea LF en todo el repo (.gitattributes), porque los
  contenedores son Linux.
- Postgres = fuente de verdad; Redis = solo la cola. La API crea la
  fila ANTES de encolar (evita que el worker busque una fila que no
  existe). El job de RQ usa el mismo id que la fila.
- SQL directo con psycopg, sin ORM, con parámetros (%s) contra
  inyección SQL. Tabla creada al arrancar la API con CREATE TABLE IF
  NOT EXISTS (simple); pasar a Alembic cuando haya cambios de esquema.
- Tests de integración (Postgres y Redis reales, no mocks), aislados de
  los datos de desarrollo: base "deadcode_test" y Redis db 1. Cada test
  empieza con tabla y Redis vacíos. La tarea del worker se prueba
  llamándola directo (RQ no corre en Windows) con monkeypatch para
  saltarse la espera simulada.
- Seguro en conftest: antes de borrar, verifica con current_database()
  que la base termine en "_test" y que Redis no sea la db 0.
- Fallas de servicios se simulan con monkeypatch (no se apagan de
  verdad) para no afectar a otros tests.
- Test del worker real: RQ en modo burst (procesa y termina). Cerrar el
  TestClient ANTES de work(): hace fork y con hilos vivos puede
  bloquearse (Python avisa con DeprecationWarning).
- CI: GitHub Actions en ubuntu-latest; Postgres y Redis como service
  containers (mismas imágenes que Compose); credenciales solo de
  prueba en el workflow; permisos mínimos (contents: read); acciones
  con versión fija (checkout@v7, setup-uv@v7, uv 0.13.8).
- GHCR: se publica solo si tests y docker-build pasan, y solo en push
  a main (no en PRs). Login con GITHUB_TOKEN (temporal, sin secretos
  guardados); solo el job publish tiene packages: write. Etiquetas:
  sha-<commit> (inmutable, para desplegar) y latest (cómoda, cambia).
  Caché de capas con type=gha.
- Probar en Linux sin CI: contenedor python:3.12-slim temporal en la
  red de Compose (POSTGRES_HOST=postgres, REDIS_HOST=redis).
- SSRF: LISTA BLANCA (solo https://github.com/<dueño>/<repo>, regex
  con fullmatch sobre toda la URL), no lista negra de IPs (siempre se
  escapa algún truco, ej. 169.254.169.254 escrito como 2852039166).
  Se rechazan usuario:clave@, puertos, ?query, #fragmento, rutas
  extra, "." y ".." (los nombres se usarán como carpetas al clonar).
- conftest: si Postgres no responde, pytest.exit con mensaje claro
  (evita 50+ errores y ~2 min de espera).
- Docker Desktop instalado por usuario en
  %LOCALAPPDATA%\Programs\DockerDesktop (no en Program Files). Tras
  reiniciar el PC hay que abrirlo antes de docker compose up.
- En tests locales usar 127.0.0.1, NO "localhost": en Windows,
  localhost prueba primero IPv6 (::1) y pierde ~2 s por conexión.

## Decisiones del análisis real
- Descarga del .tar.gz (codeload.github.com/<dueño>/<repo>/tar.gz/HEAD)
  en vez de git clone: sin git en la imagen, una sola petición HTTPS,
  tamaño controlable. URL armada por nosotros; el worker re-valida la
  URL (defensa en profundidad: el job viene de Redis).
- Redirecciones solo a codeload.github.com / github.com.
- Límites: 50 MB descarga, 300 MB / 20.000 archivos .py extraídos
  (bombas de descompresión). Solo se extraen archivos regulares .py;
  tarfile filter="data". Carpeta temporal que se borra sola.
- Commit analizado: GitHub lo pone en la cabecera pax ("comment").
- Errores esperados (errors.AnalysisError): mensaje claro al usuario,
  no se relanzan. Inesperados: "Error inesperado (<tipo>)" al usuario,
  detalle en logs (se relanzan para que RQ los registre).
- Vulture fijado en ==2.16 (resultados reproducibles). Reglas por
  framework (fastapi, flask, django, pydantic, pytest) en
  analyzers/python_static.py, activadas solo si el código las importa.
- Tests sin internet: tarballs armados en memoria (tests/helpers.py) y
  monkeypatch de fetcher._http_get.
- La confianza de Vulture NO es probabilidad de código muerto: su 100%
  suele ser un parámetro obligatorio (exc_type en __exit__, owner en
  __get__, sender en señales). Por eso clasificamos con reglas propias:
  low = fuera del código (tests/docs/examples), parámetro sin usar
  (detectado con AST) o __dunder__; high = inalcanzable, import sin
  usar, o privado (_x) sin usos; medium = público sin usos (puede ser
  API para otros proyectos). Orden de resultados: por prioridad.
- Vulture ya ignora imports en __init__.py (re-exportaciones).
- SANDBOX: el worker descarga (código nuestro) y pasa el .tar.gz por
  stdin a un contenedor desechable que extrae y analiza (código ajeno),
  y devuelve JSON por stdout. Misma imagen que la app, comando
  "python -m deadcode_service.sandbox_main" (la imagen no tiene
  secretos: llegan solo como variables al worker/API). Flags:
  --rm -i --network none --read-only --tmpfs /tmp (noexec, 512m)
  --user 65534 --cap-drop ALL no-new-privileges --memory 512m (=swap)
  --cpus 1 --pids-limit 128 --pull never; timeout 120 s (luego
  docker rm -f). Exit 137 = sin memoria. SandboxError (inesperado) NO
  es AnalysisError: el usuario ve "Error inesperado".
- SANDBOX_MODE: "docker" en Compose; "local" (sin aislamiento, mismo
  proceso) en tests y Windows. conftest fuerza "local".
- Socket de Docker = control total (admin del PC). Nadie lo recibe
  directo: "portero" tecnativa/docker-socket-proxy:v0.5.0 con
  CONTAINERS=1 y POST=1, en red "docker-control" (internal) donde solo
  está el worker; la API no lo ve. El worker usa DOCKER_HOST y el CLI
  docker:29.8.1-cli copiado en el Dockerfile.
- Límite conocido: con CONTAINERS+POST el worker podría crear
  contenedores privilegiados si lo comprometieran (el proxy filtra por
  ruta, no por contenido). Mitigación: el worker no ejecuta código
  ajeno. En Kubernetes (etapa 4) se resolverá con Jobs + Pod Security
  "restricted" + NetworkPolicy.
- Falta en CI: test del sandbox real con Docker (hoy solo en vivo).
- Kubernetes: Secrets NO van en git (base64 no es cifrado); se crean
  por comando. Postgres = StatefulSet (identidad + disco propio);
  Redis/API/worker = Deployment (intercambiables). PGDATA en subcarpeta
  del volumen. readinessProbe (¿listo? -> recibe tráfico) y
  livenessProbe (¿vivo? -> si no, reinicio). requests/limits en todo.
- K8s: DATABASE_URL se arma con $(POSTGRES_USER)/$(POSTGRES_PASSWORD)
  de variables definidas antes (desde el Secret): la contraseña nunca
  se escribe en un archivo. Imagen con etiqueta sha-<commit>, nunca
  latest. readinessProbe = /health (deps); livenessProbe = tcpSocket
  (solo el proceso): si liveness revisara Postgres, una caída de
  Postgres reiniciaría todas las APIs. API y worker: uid 1000,
  readOnlyRootFilesystem + emptyDir en /tmp, drop ALL, seccomp.
- Helm: todos los valores en values.yaml (imagen, réplicas, recursos,
  modo sandbox); NUNCA contraseñas (el chart solo nombra el Secret
  existente). _helpers.tpl tiene el bloque de DATABASE_URL una sola vez,
  etiquetas estándar app.kubernetes.io/* y la seguridad común. El
  selector NO incluye la versión (si la incluyera, cada actualización
  perdería sus pods). checksum/config reinicia pods si cambia el
  ConfigMap. Nombres fijos (api, worker, postgres, redis): un release
  por namespace.
- Sandbox en K8s: Pod (no Job) con restartPolicy Never y
  activeDeadlineSeconds: necesitamos stdin interactivo, y un Job
  reintenta (no queremos reintentar código ajeno). Diferencias con
  Docker: no hay --pids-limit por pod (es config del kubelet) ni
  noexec en /tmp (emptyDir en memoria). Ventaja: Pod Security
  "restricted" lo impone Kubernetes, así que un worker comprometido NO
  puede crear pods privilegiados (el límite del proxy de Docker
  desaparece). `kubectl run -i` necesita -q para que stdout tenga solo
  el JSON. En PowerShell 5.1 NO pasar binarios por tubería (|, <):
  los corrompe; usar Git Bash o Python.
- Usuarios de imágenes alpine: postgres = uid 70:70 (¡no 999, ese es
  Debian!), redis = 999:1000. Verificar siempre con
  `docker run --rm --entrypoint sh <imagen> -c 'grep ^<user>: /etc/passwd'`.
- Qué detecta Vulture: definiciones con nombre (import, variable,
  attribute, method, class, function, property), parámetros (como
  "variable") y código inalcanzable. NO detecta archivos completos sin
  importar ni ramas de if que nunca se cumplen (eso es coverage.py).

## Niveles de análisis (plan acordado)
1. Estático (Vulture, con ignores por framework: --ignore-decorators
   para FastAPI/Flask/Django/pytest). Funciona con cualquier repo.
2. Dinámico con los tests del repo (coverage.py), en contenedor aislado.
   Mide "qué no ejecutan los tests", no necesariamente código muerto.
3. Dinámico con tráfico E2E (idea del usuario): levantar la app con
   coverage.py y generar tráfico (Playwright para web, Schemathesis
   para APIs con OpenAPI). Inviable para repos arbitrarios (no se sabe
   arrancarlos, piden BD/claves, login bloquea al robot); viable con
   opt-in (.deadcode.yml con start/url/escenarios), APIs con OpenAPI o
   la propia app como demo.
3+. Etapa 8: agente de IA que explora la app; métrica = % de cobertura
   alcanzada vs. robot al azar.
Cruce final: Vulture "no se usa" + coverage "nunca se ejecutó" = muy
probablemente muerto; Vulture "no se usa" + "sí se ejecutó" = falso
positivo; "se usa" + "nunca se ejecutó" = vivo pero sin tests.

## Aprendizajes de Vulture (2.16)
- Probado sobre este repo y sobre pallets/itsdangerous: TODOS los
  hallazgos de 60% fueron falsos positivos. Causas: frameworks que
  llaman por decoradores (FastAPI, Pydantic, pytest), API pública de
  librerías, parámetros obligatorios sin usar (cls, kwargs).
- Compara por NOMBRE: si una función muerta se llama igual que otra
  usada, no la reporta (falso negativo). Por eso se combina con
  coverage.py.
- Código de salida 3 = "encontró algo", no es error.
- Para la parte 2: usar la API de Python (Vulture().scavenge() +
  get_unused_code()), no parsear el texto; guardar TODOS los hallazgos
  con su confianza y dejar que quien consulta filtre.
- Contenedor de análisis usado en la exploración: --user 65534,
  --cap-drop ALL, --security-opt no-new-privileges, --read-only,
  --tmpfs /tmp, sin volúmenes; tarfile con filter="data".

## Aprendizajes de coverage.py (exploración del nivel 2)
Probado a mano (contenedores desechables, scripts en el scratchpad, sin
cambiar el proyecto) con itsdangerous, click, requests y este repo.
- Flujo en 2 fases que FUNCIONA: (1) con red: leer pyproject.toml con
  tomllib (sin ejecutar nada) y `pip download --only-binary=:all:`
  (solo wheels: no ejecuta código del repo); (2) SIN red, nobody, solo
  lectura: venv en /tmp, `pip install --no-index --find-links`, repo
  instalado con `pip install -e .` y `coverage run --source=src -m pytest`.
- Dependencias de test: estándar moderno = [project.dependencies] +
  [dependency-groups] (grupo "tests"/"test"/"dev"; puede tener
  {include-group}). "requests[socks]" dentro de requests = referencia a
  sí mismo -> usar sus extras. Repos solo con requirements.txt: no
  soportados aún (y esos archivos admiten trucos: --index-url, -e, URLs).
- PYTHONPATH=src NO basta: click usa importlib.metadata (pide su propia
  versión) -> hay que instalar el repo; su empaquetador
  (build-system.requires: flit_core, setuptools, uv_build) se descarga
  en la fase 1 como wheel.
- Resultados: itsdangerous 297 OK, 97,1%; click 2604 OK + 24 fallan
  (piden el programa "less"), 85,9%; requests 615 OK + 4 fallan (sin
  red), 87,9%, pero 86 s de tests (más que el plazo actual de 120 s
  contando todo); este repo: 0% (pide Postgres: KeyError POSTGRES_USER).
  Tests que fallan NO impiden medir: la cobertura igual sirve.
- Tiempos: descarga 6-24 s, instalar 8-14 s, wheels 2-18 MB.
- ⚠️ TRAMPA del cruce: la línea de un def/import/"x = 1" SIEMPRE se
  ejecuta al importar el módulo. Para funciones/métodos hay que mirar el
  CUERPO (ast: body[0].lineno .. end_lineno). Para imports, variables y
  atributos coverage NO puede opinar (ejecutar no es leer).
- El cruce SÍ aporta: click _wrap_chunks (Vulture "high") se ejecutó
  -> sobrescribe un método de textwrap y lo llama la librería estándar
  (falso positivo); requests MockRequest.get_full_url & co. los llama
  http.cookiejar (falsos positivos); requests _find "high" nunca se
  ejecutó y nadie lo llama (muerto confirmado). En click/requests
  coverage opinó sobre ~11-22 de 137/88 hallazgos (solo funciones y
  métodos de src/); el resto: "no aplica" o fuera de src.
- Seguridad de la fase 1: un requisito puede ser una URL ("pkg @
  https://...") -> pip download la visitaría: el pod de descarga NO debe
  ver la red interna del clúster (solo internet) -> confirma la opción A.

## Estructura
- src/deadcode_service/main.py: API FastAPI (/health, POST /analyses,
  GET /analyses, GET /analyses/{id})
- src/deadcode_service/db.py: tabla "analyses" y consultas SQL
- src/deadcode_service/validation.py: validación de URLs (anti-SSRF)
- src/deadcode_service/tasks.py: tarea del worker (descargar + analizar)
- src/deadcode_service/fetcher.py: descarga segura del .tar.gz
- src/deadcode_service/analyzers/python_static.py: Vulture + reglas
  por framework (interfaz común: analyze(carpeta) -> dict)
- src/deadcode_service/analyzers/python_classify.py: prioridad,
  ubicación, categoría y motivo de cada hallazgo
- src/deadcode_service/analyzers/python_cross.py: cruce Vulture +
  coverage (nivel 2): confirma o descarta cada hallazgo
- src/deadcode_service/errors.py: AnalysisError (errores esperados)
- src/deadcode_service/sandbox.py: lanza el contenedor desechable
  (lado worker) con todas las protecciones
- src/deadcode_service/sandbox_main.py: programa DENTRO del sandbox
  (stdin .tar.gz -> stdout JSON)
- src/deadcode_service/wait_no_network.py: "portero" (initContainer)
  del sandbox en K8s: confirma que no hay red antes del análisis
- src/deadcode_service/dependencies.py: requisitos de test de un repo
  (lee su pyproject.toml, nunca ejecuta nada; nivel 2)
- src/deadcode_service/downloader_main.py: programa DENTRO del pod de
  descarga (stdin .tar.gz -> stdout .tar con las wheels; nivel 2)
- src/deadcode_service/coverage_main.py: programa DENTRO del pod de
  tests (repo + wheels por stdin -> instala SIN red, corre los tests con
  coverage -> JSON; nivel 2)
- src/deadcode_service/worker.py: worker RQ que escucha la cola
- src/deadcode_service/metrics.py: métricas para Prometheus (GET
  /metrics + middleware que cuenta y mide cada petición)
- src/deadcode_service/analysis_metrics.py: métricas de los análisis
  (cola, estados, fallos por motivo, duración), leídas de Postgres y
  Redis cuando Prometheus visita /metrics
- tests/conftest.py: preparación común (base de test, limpieza, cliente)
- tests/test_*.py: tests de health, endpoints de análisis, tarea,
  validación de URLs, descarga, analizador y worker real
  (test_worker.py, solo Linux)
- tests/helpers.py: make_tarball() para simular descargas de GitHub
- .github/workflows/ci.yml: CI (tests + build de imagen + Compose) y
  publicación de la imagen en GHCR
- src/deadcode_service/__init__.py: vacío (marca el paquete)
- pyproject.toml y uv.lock: dependencias (gestionadas con uv)
- Dockerfile: receta de la imagen de la API
- .dockerignore: lo que no entra a la imagen (.venv, .git, .env)
- docker-compose.yml: API + worker + Postgres + Redis + docker-proxy
  (portero del socket) para desarrollo
- k8s/kind-config.yaml: definición del clúster local (kind)
- argocd/deadcode.yaml: Application de ArgoCD (qué chart vigilar y dónde
  desplegarlo; se aplica una vez a mano)
- argocd/monitoring.yaml: Application de monitoreo (chart oficial
  kube-prometheus-stack + nuestros ajustes; se aplica una vez a mano)
- charts/deadcode-service/: chart de Helm (Chart.yaml, values.yaml,
  templates/ con config, api, worker, postgres, redis, sandbox,
  servicemonitor, dashboard y _helpers.tpl; dashboards/deadcode.json
  GENERADO por scripts/make_dashboard.py)
- charts/deadcode-service/alerts/deadcode.rules.yaml: reglas de alerta
  (formato Prometheus); tests/alerts/deadcode.rules.test.yaml: sus tests
  con promtool (comando en la cabecera del archivo)
- scripts/make_dashboard.py: fuente del dashboard de Grafana (cambiar
  aquí y ejecutar: uv run python scripts/make_dashboard.py --write)
- .env.example: plantilla de variables (copiar a .env)

## Comandos útiles
- Instalar dependencias (tras clonar o cambiar uv.lock): uv sync
- Ejecutar la API: uv run uvicorn deadcode_service.main:app --reload
- Probar: abrir http://127.0.0.1:8000/health o http://127.0.0.1:8000/docs
- Agregar dependencia: uv add <paquete>
- Agregar dependencia de desarrollo: uv add --dev <paquete>
- Construir imagen: docker build -t deadcode-service:dev .
- Descargar la imagen publicada:
  docker pull ghcr.io/jjorge11/deadcode-service:latest
- Ejecutar contenedor: docker run --rm -p 8000:8000 deadcode-service:dev
- Primera vez: copy .env.example .env (y cambiar la contraseña)
- Levantar todo: docker compose up -d --build
- Ver estado / logs: docker compose ps / docker compose logs -f api
  (o -f worker)
- Correr tests (requiere docker compose up -d): uv run pytest
- ⚠️ Antes de push, correr TODA la suite en Linux (en Windows se saltan
  4 tests, ej. el del worker real; pasó: CI rojo en 2626c86). Git Bash,
  con Compose arriba y la imagen deadcode-service:dev construida:
  export MSYS_NO_PATHCONV=1; net=$(docker network ls --format '{{.Name}}' | grep deadcode-service_default)
  docker run --rm --network "$net" --env-file .env -e POSTGRES_HOST=postgres -e REDIS_HOST=redis -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uvcache -v "$PWD:/w:ro" -w /w --entrypoint sh deadcode-service:dev -c 'uv sync --frozen -q; /tmp/venv/bin/python -m pytest -q -p no:cacheprovider'
- Probar un análisis: abrir http://127.0.0.1:8000/docs, POST /analyses
- Ver la tabla en Postgres:
  docker compose exec postgres psql -U deadcode -d deadcode -c "SELECT id, repo_url, status FROM analyses"
- Ver sandboxes en ejecución: docker ps --filter label=deadcode.sandbox=true
- Kubernetes: crear clúster: kind create cluster --config k8s/kind-config.yaml
  / borrarlo: kind delete cluster --name deadcode
  / ver nodos y pods: kubectl get nodes ; kubectl get pods -A
- K8s, primera vez (namespace + Secret con contraseña aleatoria):
  kubectl create namespace deadcode
  $pw = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 24 | % {[char]$_})
  kubectl create secret generic postgres-credentials -n deadcode --from-literal=POSTGRES_USER=deadcode --from-literal=POSTGRES_DB=deadcode --from-literal=POSTGRES_PASSWORD=$pw
- K8s CON ArgoCD (actual): desplegar = commit + push a main (ArgoCD
  revisa cada ~3 min); volver atrás = git revert. Estado:
  kubectl get application deadcode -n argocd
  / bootstrap (solo la primera vez): kubectl apply -f argocd/deadcode.yaml
- K8s SIN ArgoCD (antes de la etapa 5, o para probar imágenes locales
  quitando antes la Application): helm upgrade --install deadcode charts/deadcode-service -n deadcode --wait
  / historial: helm history deadcode -n deadcode
  / volver atrás: helm rollback deadcode <revisión> -n deadcode
  / revisar el chart: helm lint charts/deadcode-service --strict
  / estado: kubectl get pods -n deadcode
  / API del clúster: http://127.0.0.1:8080/docs (Compose sigue en :8000)
  / logs: kubectl logs -n deadcode deploy/worker -f
  / permisos del worker: kubectl auth can-i create pods -n deadcode-sandboxes --as=system:serviceaccount:deadcode:worker
  / sandboxes vivos: kubectl get pods -n deadcode-sandboxes
  / ArgoCD, instalar: kubectl create namespace argocd ; kubectl apply -n argocd --server-side --force-conflicts -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.6.1/manifests/install.yaml
  / ArgoCD, abrir la web (dejar la terminal abierta): kubectl port-forward -n argocd svc/argocd-server 8443:443  -> https://127.0.0.1:8443
  / ArgoCD, contraseña de admin (PowerShell):
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}')))
  / Monitoreo, primera vez (antes de aplicar argocd/monitoring.yaml):
    kubectl create namespace monitoring
    $pw = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 24 | % {[char]$_})
    kubectl create secret generic grafana-admin -n monitoring --from-literal=admin-user=admin --from-literal=admin-password=$pw
    kubectl apply -f argocd/monitoring.yaml
  / Grafana (dejar la terminal abierta): kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80  -> http://127.0.0.1:3000 (usuario admin)
  / Grafana, contraseña (PowerShell):
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((kubectl get secret grafana-admin -n monitoring -o jsonpath='{.data.admin-password}')))
  / Prometheus: kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090  -> http://127.0.0.1:9090
    (alertas: http://127.0.0.1:9090/alerts)
  / Alertmanager: kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093  -> http://127.0.0.1:9093
  / probar las alertas: docker run --rm -v "${PWD}:/w" -w /w --entrypoint promtool quay.io/prometheus/prometheus:v3.16.0 test rules tests/alerts/deadcode.rules.test.yaml
  / probar una imagen local SIN publicarla (OJO: con ArgoCD activo, selfHeal
    lo revertiría; antes quitar "automated" de la Application):
    docker build -t deadcode-service:prueba . ; kind load docker-image deadcode-service:prueba --name deadcode
    helm upgrade deadcode charts/deadcode-service -n deadcode --set image.repository=deadcode-service --set image.tag=prueba --wait
  / límite de PIDs en un clúster YA creado (Git Bash; repetir por nodo:
    deadcode-worker y deadcode-control-plane; los pods siguen vivos):
    export MSYS_NO_PATHCONV=1   (si no, Git Bash cambia /var/... por C:/Program Files/Git/var/...)
    docker exec <nodo> sh -c 'grep -q "^podPidsLimit:" /var/lib/kubelet/config.yaml || echo "podPidsLimit: 1024" >> /var/lib/kubelet/config.yaml'
    docker exec <nodo> systemctl restart kubelet
    comprobar: kubectl get --raw /api/v1/nodes/<nodo>/proxy/configz  (buscar podPidsLimit)
  / si "helm" o "kind" no se reconocen: abrir una terminal nueva (el
    PATH se actualizó al instalarlos con winget)
- GitHub CLI (gh 2.104, instalado con winget; sesión iniciada como
  JJorge11, token en el Administrador de credenciales de Windows; git
  sigue usando su credential.helper "manager", sin cambios):
  / últimos CI: gh run list --limit 5
  / jobs de un run: gh run view <id>   (log de lo que falló: gh run view <id> --log-failed)
  / esperar a que termine: gh run watch <id> --exit-status
  / sin sesión la API de GitHub permite 60 consultas/hora (se agotaron
    esperando el CI); con gh, 5000/hora.
  / ⚠️ PowerShell 5.1 corta mal los argumentos con espacios/comillas al
    pasarlos a gh (--jq '...'): usar Git Bash para esos comandos.
- Apagar (conserva datos): docker compose down
- Apagar y BORRAR datos de Postgres: docker compose down -v

## Reglas de trabajo
- Soy junior: TODAS las explicaciones deben ser a nivel junior, breves
  y claras. Definir cada término técnico la primera vez que aparece
  (en una línea) y preferir ejemplos simples a teoría.
- Explicar el "por qué" de cada cambio antes de hacerlo.
- Ir paso a paso, una etapa a la vez.
- Nunca subir credenciales ni archivos .env.
- No incluir código de empresas privadas.
- Solo analizar repos públicos, siempre dentro de contenedores.
- Entorno: Windows + PowerShell, Python 3.12 con uv.
- Mensajes de commit claros, en español, en imperativo
  (por ejemplo: "Agrega endpoint /health").