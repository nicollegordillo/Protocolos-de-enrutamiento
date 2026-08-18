

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import transport
from .config import NodeConfig


@dataclass
class BankResponse:
    action: str
    data: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {"action": self.action, "data": self.data}


class ATMClient:

    def __init__(self, cfg: NodeConfig, bank_id: str = "servidor_bancario"):
        if cfg.role != "client":
            raise ValueError("ATMClient requiere una configuración role='client'")
        self.cfg = cfg
        self.bank_id = bank_id
        self._server = None
        self._responses: list[dict[str, Any]] = []
        self._response_event = threading.Event()
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def _on_line(self, line: str, addr: tuple) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(f"[atm] respuesta inválida desde {addr}: {line[:100]!r}")
            return
        if message.get("type") != "MESSAGE":
            print(f"[atm] mensaje inesperado: {message}")
            return
        payload = message.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {"action": "error", "data": {"message": payload}}
        if not isinstance(payload, dict):
            payload = {"action": "error", "data": {"message": "Respuesta inválida"}}
        with self._lock:
            self._responses.append(payload)
            self._response_event.set()
        print(f"[atm] respuesta de {message.get('from')}: {payload}")

    def start_listener(self) -> None:
        self._server = transport.LineServer(
            self.cfg.listen_ip,
            self.cfg.listen_port,
            self._on_line,
            name=f"{self.cfg.node_id}-atm-listener",
        )
        self._server.start()
        print(
            f"[atm] escuchando en {self.cfg.listen_ip}:{self.cfg.listen_port}; "
            f"gateway={self.cfg.gateway.ip}:{self.cfg.gateway.port}; banco={self.bank_id}"
        )

    def stop(self) -> None:
        self._stopping.set()
        if self._server:
            self._server.stop()

    def request(self, action: str, data: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        packet = transport.message(
            self.cfg.node_id,
            self.bank_id,
            {"action": action, "data": data},
            hops=0,
        )
        self._response_event.clear()
        transport.send_json(self.cfg.gateway.ip, self.cfg.gateway.port, packet)
        print(
            f"[atm] operación {action} enviada a {self.bank_id} "
            f"vía gateway {self.cfg.gateway.node_id}"
        )
        if not self._response_event.wait(timeout):
            raise TimeoutError("no se recibió respuesta del servidor bancario")
        with self._lock:
            return self._responses.pop(0)

    def run_interactive(self) -> None:
        self.start_listener()
        logged_in = False
        try:
            while not self._stopping.is_set():
                if not logged_in:
                    tarjeta = input("\nNúmero de tarjeta: ").strip()
                    pin = input("PIN: ").strip()
                    response = self.request("login", {"tarjeta": tarjeta, "pin": pin})
                    if response.get("action") == "login_ok":
                        logged_in = True
                    continue

                print("\n--- MENU ATM ---\n1) Retirar dinero\n2) Logout")
                choice = input("Elige una opción: ").strip()
                if choice == "1":
                    try:
                        cantidad = float(input("Cantidad de retiro: ").strip())
                    except ValueError:
                        print("Monto inválido")
                        continue
                    response = self.request("retiro", {"cantidad": cantidad})
                    print(f"[atm] resultado: {response}")
                elif choice == "2":
                    response = self.request("logout", {})
                    print(f"[atm] resultado: {response}")
                    break
        except (EOFError, KeyboardInterrupt):
            print("\n[atm] detenido")
        finally:
            self.stop()


class ATMServer:
    ACCOUNTS: dict[str, dict[str, Any]] = {
        "4111111111111111": {"pin": "1234", "balance": 5000.00},
        "5500005555555559": {"pin": "0000", "balance": 12000.50},
        "22523": {"pin": "4444", "balance": 30000.00},
        "22246": {"pin": "2222", "balance": 30000.00},
    }

    def __init__(self, cfg: NodeConfig):
        if cfg.role != "server":
            raise ValueError("ATMServer requiere una configuración role='server'")
        self.cfg = cfg
        self._server = None
        self._sessions: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self._server = transport.LineServer(
            self.cfg.listen_ip,
            self.cfg.listen_port,
            self._on_line,
            name=f"{self.cfg.node_id}-bank-listener",
        )
        self._server.start()
        print(
            f"[banco] escuchando en {self.cfg.listen_ip}:{self.cfg.listen_port}; "
            f"gateway={self.cfg.gateway.ip}:{self.cfg.gateway.port}"
        )
        try:
            while True:
                time.sleep(0.5)
        except (KeyboardInterrupt, EOFError):
            self.stop()

    def stop(self) -> None:
        if self._server:
            self._server.stop()

    @staticmethod
    def _payload_dict(message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("payload bancario debe ser un objeto JSON")
        return payload

    def _reply(self, request: dict[str, Any], action: str, data: dict[str, Any]) -> None:
        destination = request.get("from")
        if not destination:
            return
        packet = transport.message(
            self.cfg.node_id,
            destination,
            BankResponse(action, data).to_payload(),
            hops=0,
        )
        transport.send_json(self.cfg.gateway.ip, self.cfg.gateway.port, packet)
        print(f"[banco] respuesta {action} enviada a {destination} vía gateway")

    def _on_line(self, line: str, addr: tuple) -> None:
        try:
            message = json.loads(line)
            if message.get("type") != "MESSAGE":
                raise ValueError("se esperaba type=MESSAGE")
            payload = self._payload_dict(message)
            action = payload.get("action")
            data = payload.get("data", {})
            origin = str(message.get("from"))
            print(f"[banco] operación recibida de {origin}: {payload}")

            with self._lock:
                authenticated = self._sessions.get(origin)
                if action == "login":
                    tarjeta = str(data.get("tarjeta", ""))
                    pin = str(data.get("pin", ""))
                    account = self.ACCOUNTS.get(tarjeta)
                    if account and account["pin"] == pin:
                        self._sessions[origin] = tarjeta
                        response = BankResponse("login_ok", {"message": "Autenticación exitosa"})
                    else:
                        response = BankResponse(
                            "login_denegado", {"message": "Tarjeta o PIN incorrectos"}
                        )
                elif action == "retiro":
                    if authenticated is None:
                        response = BankResponse("error", {"message": "No autenticado"})
                    else:
                        try:
                            amount = float(data.get("cantidad", 0))
                        except (TypeError, ValueError):
                            amount = 0
                        account = self.ACCOUNTS[authenticated]
                        if amount <= 0:
                            response = BankResponse("error", {"message": "Monto inválido"})
                        elif amount > account["balance"]:
                            response = BankResponse("error", {"message": "Fondos insuficientes"})
                        else:
                            account["balance"] -= amount
                            response = BankResponse(
                                "retiro_ok",
                                {"cantidad": amount, "balance": account["balance"]},
                            )
                elif action == "logout":
                    self._sessions.pop(origin, None)
                    response = BankResponse("logout_ok", {"message": "Hasta luego"})
                else:
                    response = BankResponse("error", {"message": "Acción desconocida"})

            self._reply(message, response.action, response.data)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            print(f"[banco] mensaje descartado: {exc}")
            try:
                request = json.loads(line)
                self._reply(request, "error", {"message": str(exc)})
            except Exception:
                pass


def run_atm_client(cfg: NodeConfig, bank_id: str) -> None:
    ATMClient(cfg, bank_id=bank_id).run_interactive()


def run_atm_server(cfg: NodeConfig) -> None:
    ATMServer(cfg).start()
