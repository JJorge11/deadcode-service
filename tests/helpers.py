"""Utilidades compartidas por los tests."""

import io
import tarfile


def make_tarball(files: dict[str, str], root: str = "repo-abc123", commit: str | None = "abc123") -> bytes:
    """Arma en memoria un .tar.gz como los que entrega GitHub.

    files: {"ruta/archivo.py": "contenido"}. Todo queda dentro de la
    carpeta raíz `root`, y el SHA del commit en la cabecera (pax).
    """
    buffer = io.BytesIO()
    pax_headers = {"comment": commit} if commit else {}
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax_headers) as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}" if root else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
