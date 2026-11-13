# Imagen base: Debian reducido ("slim") con Python 3.12 ya instalado.
FROM python:3.12-slim

# Copiamos el ejecutable de uv desde su imagen oficial (misma versión que en local).
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

# Configuración de uv dentro del contenedor:
# - COMPILE_BYTECODE: precompila los .py para que la API arranque más rápido.
# - LINK_MODE=copy: copia archivos en vez de enlazarlos (evita avisos en Docker).
# - PYTHON_DOWNLOADS=0: usar el Python de la imagen, nunca descargar otro.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Carpeta de trabajo dentro del contenedor (como hacer "cd /app").
WORKDIR /app

# 1) Primero SOLO los archivos de dependencias. Docker guarda cada paso en
#    caché: si no cambian, no reinstala librerías cuando solo cambias código.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Ahora el código, e instalamos nuestro propio paquete.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Usamos el Python del entorno virtual creado por uv.
ENV PATH="/app/.venv/bin:$PATH"

# Por seguridad, no ejecutamos como "root" (administrador).
RUN useradd --uid 1000 app
USER app

# Documenta el puerto donde escucha la API.
EXPOSE 8000

# Comando al iniciar el contenedor. 0.0.0.0 = aceptar conexiones desde
# fuera del contenedor (con 127.0.0.1 solo se escucharía a sí mismo).
CMD ["uvicorn", "deadcode_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
