"""
Capa de transporte + framing.

Regla acordada: cada mensaje viaja como una linea terminada en '\n' sobre TCP.
  - Mensajes de control (HELLO, HELLO_ACK, LSA) -> JSON en texto plano UTF-8.
  - Mensajes de datos entre routers            -> cadena de bits Hamming(7,4).
  - Tramo router-gateway <-> client/server      -> JSON en texto plano.

Se distingue el tipo de linea al recibirla: si solo contiene '0' y '1' es una
trama de bits; en caso contrario se parsea como JSON.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable

DELIMITER = "\n"
ENCODING = "utf-8"
CONNECT_TIMEOUT = 3.0
RECV_CHUNK = 8192


# --------------------------------------------------------------------------- #
# constructores de mensajes (nombres de campo acordados entre las 3 parejas)
# --------------------------------------------------------------------------- #
def hello(from_id: str, to_id: str) -> dict:
    return {"type": "HELLO", "from": from_id, "to": to_id}


def hello_ack(from_id: str, to_id: str) -> dict:
    return {"type": "HELLO_ACK", "from": from_id, "to": to_id}


def lsa(origin: str, seq: int, links: list[dict], from_id: str) -> dict:
    return {
        "type": "LSA",
        "origin": origin,
        "seq": seq,
        "links": links,
        "from": from_id,
    }


def message(from_id: str, to_id: str, payload: str, hops: int = 0) -> dict:
    return {
        "type": "MESSAGE",
        "from": from_id,
        "to": to_id,
        "hops": hops,
        "payload": payload,
    }


def dumps(obj: dict) -> str:
    """JSON compacto, sin escapar acentos, en una sola linea."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# envio
# --------------------------------------------------------------------------- #
def send_line(ip: str, port: int, line: str, timeout: float = CONNECT_TIMEOUT,
              read_reply: bool = False, reply_timeout: float = 0.6) -> str | None:
    """
    Abre una conexion TCP, envia una linea y la cierra. Lanza OSError si falla.

    Si read_reply es True espera brevemente por una respuesta en el MISMO socket
    antes de cerrar. Nuestra implementacion siempre responde abriendo una conexion
    nueva, pero otra pareja podria contestar sobre la conexion entrante (es lo
    natural en TCP); sin esta espera, ese HELLO_ACK se perderia y creeriamos que
    el vecino esta caido.
    """
    with socket.create_connection((ip, port), timeout=timeout) as sock:
        sock.sendall((line + DELIMITER).encode(ENCODING))
        if not read_reply:
            return None
        sock.settimeout(reply_timeout)
        buffer = ""
        try:
            while DELIMITER not in buffer:
                chunk = sock.recv(RECV_CHUNK)
                if not chunk:
                    break
                buffer += chunk.decode(ENCODING, errors="replace")
        except (socket.timeout, OSError):
            pass
        reply = buffer.split(DELIMITER, 1)[0].strip()
        return reply or None


def send_json(ip: str, port: int, obj: dict, timeout: float = CONNECT_TIMEOUT,
              read_reply: bool = False) -> str | None:
    return send_line(ip, port, dumps(obj), timeout, read_reply)


# --------------------------------------------------------------------------- #
# recepcion
# --------------------------------------------------------------------------- #
class LineServer:
    """
    Servidor TCP que acepta conexiones y entrega cada linea recibida al callback
    `on_line(linea, direccion_remota)`. Cada conexion se atiende en su propio hilo,
    de modo que la recepcion nunca bloquea al resto del nodo.
    """

    def __init__(self, ip: str, port: int, on_line: Callable[[str, tuple], None],
                 name: str = "listener"):
        self.ip = ip
        self.port = port
        self.on_line = on_line
        self.name = name
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.ip, self.port))
        self._sock.listen(64)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._accept_loop, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn, addr), daemon=True
            ).start()

    def _handle(self, conn: socket.socket, addr: tuple) -> None:
        buffer = ""
        with conn:
            conn.settimeout(10.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(RECV_CHUNK)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buffer += chunk.decode(ENCODING, errors="replace")
                while DELIMITER in buffer:
                    line, buffer = buffer.split(DELIMITER, 1)
                    line = line.strip()
                    if line:
                        try:
                            self.on_line(line, addr)
                        except Exception as exc:  # un mensaje malo no tumba el nodo
                            print(f"[transport] error procesando linea: {exc}")
            if buffer.strip():  # ultima linea sin delimitador
                try:
                    self.on_line(buffer.strip(), addr)
                except Exception as exc:
                    print(f"[transport] error procesando linea final: {exc}")
