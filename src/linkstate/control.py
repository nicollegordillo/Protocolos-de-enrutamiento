"""
PLANO DE CONTROL — responsable: Nicolle

Todo lo que construye la tabla de enrutamiento:

    descubrimiento de vecinos (HELLO / HELLO_ACK)
    construccion del LSA propio
    algoritmo de flooding
    base de datos de estado de enlace
    Dijkstra y escritura de nodo_tabla_enrutamiento.csv

Este modulo NO sabe nada de Hamming ni de reenvio de mensajes: su unico producto
es la tabla de rutas, que el plano de datos consulta con lookup().
"""

from __future__ import annotations

import time

from . import transport
from .config import Peer
from .routing import LinkStateDB, compute_routes, format_table, write_table

HELLO_INTERVAL = 4.0        # cada cuanto se saluda a los vecinos (s)
DEAD_INTERVAL = 13.0        # sin senal de vida por este tiempo -> enlace caido (s)
LSA_REFRESH = 20.0          # reanuncio periodico del LSA propio (s)
RECOMPUTE_DEBOUNCE = 0.4    # espera antes de recalcular Dijkstra (s)


class NeighborState:
    """Estado de un enlace hacia un vecino directo."""

    __slots__ = ("peer", "alive", "last_seen", "misses")

    def __init__(self, peer: Peer):
        self.peer = peer
        self.alive = False
        self.last_seen = 0.0
        self.misses = 0


class ControlPlane:
    def __init__(self, node):
        self.node = node                      # el Router que lo hospeda
        self.cfg = node.cfg
        self.log = node.log

        self.lsdb = LinkStateDB()
        self.seq = 0
        self.neighbors: dict[str, NeighborState] = {
            n.node_id: NeighborState(n) for n in self.cfg.neighbors
        }
        self._routes = []

    # ================================================================== #
    # arranque
    # ================================================================== #
    def start(self, spawn) -> None:
        spawn("hello", self._hello_thread)
        spawn("monitor", self._monitor_thread)
        self.generate_lsa()   # LSA inicial hacia todos los vecinos

    # ================================================================== #
    # despacho de mensajes de control
    # ================================================================== #
    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        # Cualquier mensaje recibido de un vecino prueba que el enlace vive, no
        # solo el HELLO_ACK: asi toleramos que otra pareja use intervalos de
        # HELLO distintos a los nuestros sin declararla caida por error.
        self._mark_alive(msg.get("from"))

        if kind == "HELLO":
            self._handle_hello(msg)
        elif kind == "HELLO_ACK":
            pass                      # la senal de vida ya se registro arriba
        elif kind == "LSA":
            self._handle_lsa(msg)
        else:
            self.log(f"mensaje de control desconocido: {kind}")

    # ---------------------------- HELLO ------------------------------- #
    def _handle_hello(self, msg: dict) -> None:
        origin = msg.get("from")
        state = self.neighbors.get(origin)
        if state is None:
            self.log(f"HELLO de {origin}, que no esta en mi config; se ignora")
            return
        try:
            transport.send_json(state.peer.ip, state.peer.port,
                                transport.hello_ack(self.cfg.node_id, origin))
        except OSError as exc:
            self.log(f"no se pudo responder HELLO_ACK a {origin}: {exc}")

    def _mark_alive(self, node_id: str | None) -> None:
        state = self.neighbors.get(node_id)
        if state is None:
            return
        state.last_seen = time.time()
        state.misses = 0
        if not state.alive:
            state.alive = True
            self.log(f"enlace con {node_id} ARRIBA")
            self.generate_lsa()

    def _mark_down(self, node_id: str) -> None:
        state = self.neighbors[node_id]
        if state.alive:
            state.alive = False
            self.log(f"enlace con {node_id} CAIDO")
            self.generate_lsa()

    def _hello_thread(self) -> None:
        while not self.node.stopping:
            for node_id, state in self.neighbors.items():
                try:
                    reply = transport.send_json(
                        state.peer.ip, state.peer.port,
                        transport.hello(self.cfg.node_id, node_id),
                        read_reply=True,
                    )
                except OSError:
                    state.misses += 1
                    continue
                # algunas implementaciones responden sobre la misma conexion
                if reply:
                    self.node.on_line(reply, (state.peer.ip, state.peer.port))
            self.node.wait(HELLO_INTERVAL)

    def _monitor_thread(self) -> None:
        last_refresh = time.time()
        while not self.node.stopping:
            now = time.time()
            for node_id, state in self.neighbors.items():
                if state.alive and (now - state.last_seen) > DEAD_INTERVAL:
                    self._mark_down(node_id)

            if self.node.dirty.is_set():
                time.sleep(RECOMPUTE_DEBOUNCE)   # agrupa rafagas de LSA
                self.node.dirty.clear()
                self.recompute()

            if now - last_refresh >= LSA_REFRESH:
                last_refresh = now
                self.generate_lsa()

            self.node.wait(1.0)

    # ----------------------------- LSA -------------------------------- #
    def own_links(self) -> list[dict]:
        """Adyacencias vivas del nodo + hosts locales (costo 0)."""
        links = [{"to": s.peer.node_id, "cost": s.peer.cost}
                 for s in self.neighbors.values() if s.alive]
        links += [{"to": h.node_id, "cost": h.cost} for h in self.cfg.hosts]
        return links

    def generate_lsa(self, bump: bool = True) -> None:
        """Regenera el LSA propio, lo guarda en la LSDB y lo inunda."""
        if bump:
            self.seq += 1
        links = self.own_links()
        self.lsdb.update(self.cfg.node_id, self.seq, links)
        self.node.dirty.set()
        packet = transport.lsa(self.cfg.node_id, self.seq, links, self.cfg.node_id)
        self.flood(packet)
        self.log(f"LSA propio seq={self.seq} enlaces="
                 f"{[l['to'] + ':' + str(l['cost']) for l in links]}")

    def _handle_lsa(self, msg: dict) -> None:
        origin = msg.get("origin")
        seq = int(msg.get("seq", 0))
        links = msg.get("links", [])
        sender = msg.get("from")

        if origin == self.cfg.node_id:
            return  # mi propio LSA que regreso por la red: descartar

        if not self.lsdb.update(origin, seq, links):
            return  # repetido o mas viejo: descartar

        self.log(f"LSA nuevo de {origin} (seq={seq}) recibido via {sender}; se inunda")
        self.node.dirty.set()
        self.flood(transport.lsa(origin, seq, links, self.cfg.node_id), exclude=sender)

    def flood(self, packet: dict, exclude: str | None = None) -> None:
        """
        Reenvia el paquete a todos los vecinos menos por donde llego (split horizon).
        Se intenta con todos los vecinos declarados, incluso los que aun no han
        respondido HELLO_ACK: al arrancar la red nadie esta 'vivo' todavia.
        """
        for node_id, state in self.neighbors.items():
            if node_id == exclude:
                continue
            try:
                transport.send_json(state.peer.ip, state.peer.port, packet)
            except OSError:
                state.misses += 1

    # -------------------------- Dijkstra ------------------------------ #
    def recompute(self) -> None:
        routes = compute_routes(self.lsdb.snapshot(), self.cfg.node_id, self.cfg.peer)
        self._routes = routes
        write_table(routes, self.node.table_path)
        self.log(f"Dijkstra recalculado, {len(routes)} rutas -> {self.node.table_path}")
        if self.node.verbose:
            print(format_table(routes, self.cfg.node_id))

    def lookup(self, destination: str):
        """Consulta de la tabla de rutas. Es lo que usa el plano de datos."""
        for route in self._routes:
            if route.destination == destination:
                return route
        return None
