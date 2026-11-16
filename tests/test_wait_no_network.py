"""Tests del portero del sandbox (wait_no_network).

No usamos red de verdad: reemplazamos socket.create_connection por uno
falso que simula cada respuesta posible.
"""

import socket

import pytest

from deadcode_service import wait_no_network as portero


class _FakeSocket:
    def close(self):
        pass


def _fake_connection(result):
    def fake(address, timeout):
        if isinstance(result, type) and issubclass(result, BaseException):
            raise result()
        return _FakeSocket()

    return fake


# --- ¿Cuándo cuenta como "bloqueado"? ---------------------------------


@pytest.mark.parametrize(
    "result, blocked",
    [
        ("conecta", False),  # conectó: HAY red
        (ConnectionRefusedError, False),  # alguien respondió "no": HAY red
        (TimeoutError, True),  # nadie responde: bloqueado
        (OSError, True),  # "sin ruta": bloqueado
    ],
)
def test_cuando_cuenta_como_bloqueado(monkeypatch, result, blocked):
    monkeypatch.setattr(socket, "create_connection", _fake_connection(result))

    assert portero.network_is_blocked("10.96.0.1", 443) is blocked


# --- Espera hasta que se bloquee ----------------------------------------


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(portero.time, "sleep", lambda seconds: None)


def test_espera_a_que_la_red_se_bloquee(monkeypatch):
    """Lo que vimos en el clúster: abierta al nacer, bloqueada después."""
    answers = iter([False, False, True])
    monkeypatch.setattr(portero, "network_is_blocked", lambda host, port: next(answers))

    assert portero.wait_until_blocked([("10.96.0.1", 443)]) is True


def test_exige_que_todos_los_destinos_esten_bloqueados(monkeypatch):
    """Si internet sigue abierto, no basta con que la API esté bloqueada."""
    monkeypatch.setattr(portero, "network_is_blocked", lambda host, port: host != "1.1.1.1")

    assert portero.wait_until_blocked([("10.96.0.1", 443), ("1.1.1.1", 443)]) is False


def test_falla_cerrado_si_la_red_nunca_se_bloquea(monkeypatch):
    calls = []
    monkeypatch.setattr(portero, "network_is_blocked", lambda host, port: calls.append(1) or False)
    monkeypatch.setattr(portero, "targets", lambda internal_only=False: [("10.96.0.1", 443)])

    with pytest.raises(SystemExit) as exc:
        portero.main([])

    assert exc.value.code == 1  # código != 0: el análisis NO arranca
    assert len(calls) == portero.MAX_ATTEMPTS  # se rinde (no espera para siempre)


def test_termina_bien_si_la_red_esta_bloqueada(monkeypatch, capsys):
    monkeypatch.setattr(portero, "network_is_blocked", lambda host, port: True)

    portero.main([])  # no lanza SystemExit = código 0

    assert "Red bloqueada" in capsys.readouterr().out


# --- Destinos -------------------------------------------------------------


def test_destinos_incluyen_la_api_de_kubernetes(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")

    assert portero.targets() == [("10.96.0.1", 443), ("1.1.1.1", 443)]


def test_destinos_sin_kubernetes(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    assert portero.targets() == [("1.1.1.1", 443)]


# --- Modo "solo red interna" (pod de descarga, nivel 2) -------------------


def test_solo_red_interna_no_prueba_internet(monkeypatch):
    """El pod de descarga SÍ debe llegar a internet: no se exige bloquearlo."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")

    assert portero.targets(internal_only=True) == [("10.96.0.1", 443)]


def test_solo_red_interna_espera_a_que_la_api_se_bloquee(monkeypatch, capsys):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    probados = []
    answers = iter([False, True])  # abierta al nacer, bloqueada después
    monkeypatch.setattr(
        portero, "network_is_blocked", lambda host, port: probados.append(host) or next(answers)
    )

    portero.main([portero.INTERNAL_ONLY_FLAG])

    assert probados == ["10.96.0.1", "10.96.0.1"]  # nunca probó 1.1.1.1
    assert "Red interna bloqueada" in capsys.readouterr().out


def test_solo_red_interna_falla_cerrado_si_la_api_responde(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setattr(portero, "network_is_blocked", lambda host, port: False)

    with pytest.raises(SystemExit) as exc:
        portero.main([portero.INTERNAL_ONLY_FLAG])

    assert exc.value.code == 1


def test_sin_destinos_falla_cerrado(monkeypatch):
    """Fuera de Kubernetes no hay API que probar: all([]) sería True."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(portero, "network_is_blocked", lambda host, port: True)

    with pytest.raises(SystemExit) as exc:
        portero.main([portero.INTERNAL_ONLY_FLAG])

    assert exc.value.code == 1
