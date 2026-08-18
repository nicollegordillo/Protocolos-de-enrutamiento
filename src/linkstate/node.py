from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from . import dataplane, transport
from .config import NodeConfig
from .control import ControlPlane
from . import banking


class Node:
    def __init__(self, cfg: NodeConfig, table_dir: str | Path = ".", verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self.table_path = Path(table_dir) / f"{cfg.node_id}_tabla_enrutamiento.csv"

        self.stopping = False
        self.dirty = threading.Event()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._log_lock = threading.Lock()

        self.control: ControlPlane | None = None
        self.forwarding: dataplane.Forwarding | None = None
        self._server: transport.LineServer | None = None

        if cfg.role == "router":
            self.control = ControlPlane(self)
            self.forwarding = dataplane.Forwarding(self)

    def log(self, message: str) -> None:
        with self._log_lock:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] [{self.cfg.node_id}] {message}", flush=True)

    def wait(self, seconds: float) -> None:
        self._stop_event.wait(seconds)

    def spawn(self, name: str, target) -> threading.Thread:
        thread = threading.Thread(
            target=target, name=f"{self.cfg.node_id}-{name}", daemon=True
        )
        thread.start()
        self._threads.append(thread)
        return thread

    def on_line(self, line: str, addr: tuple) -> None:
        if self.cfg.role != "router":
            self._on_line_host(line, addr)
            return

        if dataplane.is_data_frame(line):
            self.forwarding.handle_frame(line, addr)
            return

        try:
            msg = json.loads(line)
        except ValueError:
            self.log(f"linea descartada, no es bits ni JSON valido ({addr}): {line[:60]!r}")
            return

        if msg.get("type") == "MESSAGE":
            self.forwarding.originate(msg["from"], msg["to"], msg["payload"], addr)
        else:
            self.control.handle(msg)

    def _on_line_host(self, line: str, addr: tuple) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            self.log(f"linea descartada, no es JSON valido ({addr}): {line[:60]!r}")
            return
        if msg.get("type") != "MESSAGE":
            self.log(f"mensaje inesperado para un nodo {self.cfg.role}: {msg}")
            return
        if self.cfg.role == "server":
            print(f"\n>>> [{time.strftime('%H:%M:%S')}] mensaje de "
                f"'{msg.get('from')}': {msg.get('payload')!r}\n")
        self.log(f"MESSAGE recibido de {msg.get('from')}: {msg.get('payload')!r}")
    def send_message(self, to_id: str, payload: str) -> None:
        if not self.cfg.neighbors:
            raise RuntimeError(f"{self.cfg.node_id} no tiene gateway configurado")
        target = self.cfg.neighbors[0]

        packet = dataplane.DataPacket(self.cfg.node_id, to_id, payload, hops=0)
        transport.send_json(target.ip, target.port, packet.to_dict())
        self.log(f"MESSAGE enviado a {to_id} via {target.node_id} ({target.ip}:{target.port})")

    def interactive_client(self) -> None:
        if self.cfg.role != "client":
            raise RuntimeError("solo para nodos role='client'")

        print(f"=== Cliente '{self.cfg.node_id}' listo (gateway: "
            f"{self.cfg.neighbors[0].node_id}) ===")
        print("Escriba 'salir' para terminar.\n")
        while not self.stopping:
            try:
                to_id = input("Destino (node_id del servidor): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if to_id.lower() == "salir":
                break
            if not to_id:
                continue
            try:
                payload = input("Mensaje: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if payload.lower() == "salir":
                break
            try:
                self.send_message(to_id, payload)
            except OSError as exc:
                self.log(f"no se pudo enviar: {exc}")
            print()

    def start(self) -> None:
        self._server = transport.LineServer(
            self.cfg.listen_ip, self.cfg.listen_port, self.on_line,
            name=f"{self.cfg.node_id}-listener",
        )
        self._server.start()
        self.log(f"escuchando en {self.cfg.listen_ip}:{self.cfg.listen_port} (rol={self.cfg.role})")

        if self.cfg.role == "router":
            self.control.start(self.spawn)
            self.log(f"tabla de rutas se escribira en {self.table_path}")

    def stop(self) -> None:
        self.stopping = True
        self._stop_event.set()
        if self._server:
            self._server.stop()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self.log("nodo detenido")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self.stopping:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Levanta un nodo de la red Link State")
    parser.add_argument("--config", required=True, help="ruta al config.json del nodo")
    parser.add_argument("--table-dir", default=".",
                        help="carpeta donde escribir nodo_tabla_enrutamiento.csv")
    parser.add_argument("--quiet", action="store_true",
                        help="no imprimir la tabla en cada recalculo de Dijkstra")
    parser.add_argument("--bank-client", action="store_true",
                        help="ejecuta el ATM bancario interactivo sobre MESSAGE")
    parser.add_argument("--bank-server", action="store_true",
                        help="ejecuta el servidor bancario sobre MESSAGE")
    parser.add_argument("--bank-id", default="servidor_bancario",
                        help="node_id del servidor bancario para el ATM")
    args = parser.parse_args(argv)

    if args.bank_client and args.bank_server:
        parser.error("--bank-client y --bank-server son excluyentes")

    cfg = NodeConfig.load(args.config)

    if args.bank_client:
        if cfg.role != "client":
            parser.error("--bank-client requiere role='client'")
        banking.run_atm_client(cfg, bank_id=args.bank_id)
        return

    if args.bank_server:
        if cfg.role != "server":
            parser.error("--bank-server requiere role='server'")
        banking.run_atm_server(cfg)
        return

    node = Node(cfg, table_dir=args.table_dir, verbose=not args.quiet)

    if cfg.role == "client":
        node.start()
        try:
            node.interactive_client()
        finally:
            node.stop()
    else:
        node.run_forever()


if __name__ == "__main__":
    main()