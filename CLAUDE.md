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
- El análisis sigue SIMULADO (tasks._run_analysis espera 5 s).
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
- Próximo (antes de Kubernetes): análisis real con Vulture dentro de un
  contenedor aislado. Ahí verificar también que el repo exista y sea
  público (la validación de URL no lo comprueba).
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

## Estructura
- src/deadcode_service/main.py: API FastAPI (/health, POST /analyses,
  GET /analyses, GET /analyses/{id})
- src/deadcode_service/db.py: tabla "analyses" y consultas SQL
- src/deadcode_service/validation.py: validación de URLs (anti-SSRF)
- src/deadcode_service/tasks.py: tareas que ejecuta el worker
- src/deadcode_service/worker.py: worker RQ que escucha la cola
- tests/conftest.py: preparación común (base de test, limpieza, cliente)
- tests/test_*.py: tests de health, endpoints de análisis, tarea,
  validación de URLs y worker real (test_worker.py, solo Linux)
- .github/workflows/ci.yml: CI (tests + build de imagen + Compose) y
  publicación de la imagen en GHCR
- src/deadcode_service/__init__.py: vacío (marca el paquete)
- pyproject.toml y uv.lock: dependencias (gestionadas con uv)
- Dockerfile: receta de la imagen de la API
- .dockerignore: lo que no entra a la imagen (.venv, .git, .env)
- docker-compose.yml: API + worker + Postgres + Redis para desarrollo
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
- Probar un análisis: abrir http://127.0.0.1:8000/docs, POST /analyses
- Ver la tabla en Postgres:
  docker compose exec postgres psql -U deadcode -d deadcode -c "SELECT id, repo_url, status FROM analyses"
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