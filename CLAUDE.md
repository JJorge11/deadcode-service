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
- Próximo: etapa 2 (tests con pytest y CI con GitHub Actions).
- Pendiente más adelante: análisis real con Vulture dentro de un
  contenedor aislado; migraciones con Alembic cuando cambie la tabla.

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

## Estructura
- src/deadcode_service/main.py: API FastAPI (/health, POST /analyses,
  GET /analyses, GET /analyses/{id})
- src/deadcode_service/db.py: tabla "analyses" y consultas SQL
- src/deadcode_service/tasks.py: tareas que ejecuta el worker
- src/deadcode_service/worker.py: worker RQ que escucha la cola
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
- Ejecutar contenedor: docker run --rm -p 8000:8000 deadcode-service:dev
- Primera vez: copy .env.example .env (y cambiar la contraseña)
- Levantar todo: docker compose up -d --build
- Ver estado / logs: docker compose ps / docker compose logs -f api
  (o -f worker)
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