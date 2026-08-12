"""
Base de datos de estado de enlace (LSDB), algoritmo de Dijkstra y generacion
del archivo nodo_tabla_enrutamiento.csv.
"""

from __future__ import annotations

import csv
import heapq
import threading
import time
from dataclasses import dataclass
from pathlib import Path

INFINITY = float("inf")
CSV_HEADER = ["destino", "siguiente_salto", "ip", "puerto", "costo"]
STALE_AFTER = 45.0   # segundos sin refresco tras los cuales un LSA se considera viejo


@dataclass
class LsaRecord:
    origin: str
    seq: int
    links: list[dict]          # [{"to": "B", "cost": 1}, ...]
    updated: float = 0.0       # momento en que se recibio (para detectar reinicios)


class LinkStateDB:
    """
    Guarda el LSA mas reciente de cada origen y decide si un LSA entrante es
    nuevo (hay que inundarlo) o viejo (hay que descartarlo).
    """

    def __init__(self, stale_after: float = STALE_AFTER):
        self._db: dict[str, LsaRecord] = {}
        self._lock = threading.Lock()
        self._stale_after = stale_after

    def update(self, origin: str, seq: int, links: list[dict]) -> bool:
        """
        Devuelve True si el LSA es nuevo o mas reciente que el almacenado
        (y por lo tanto debe reenviarse por flooding), False si se descarta.

        Excepcion importante: si el LSA almacenado ya esta viejo (no se refresca
        desde hace stale_after segundos) se acepta aunque traiga un seq menor.
        Eso pasa cuando un nodo se reinicia y su numeracion vuelve a empezar: sin
        esta regla lo ignorariamos hasta que su seq superara al que teniamos
        guardado, y ese nodo quedaria invisible para el resto de la red.
        """
        with self._lock:
            current = self._db.get(origin)
            if current is not None and seq <= current.seq:
                if (time.time() - current.updated) < self._stale_after:
                    return False
            self._db[origin] = LsaRecord(origin, seq, list(links), time.time())
            return True

    def snapshot(self) -> dict[str, LsaRecord]:
        with self._lock:
            return dict(self._db)

    def origins(self) -> list[str]:
        with self._lock:
            return sorted(self._db)

    def __len__(self) -> int:
        with self._lock:
            return len(self._db)


# --------------------------------------------------------------------------- #
# grafo + Dijkstra
# --------------------------------------------------------------------------- #
def build_graph(db: dict[str, LsaRecord]) -> dict[str, dict[str, int]]:
    """
    Construye el grafo dirigido de la red a partir de todos los LSA conocidos.
    Cada LSA aporta las aristas origin -> to con el costo anunciado por origin.
    Si el mismo enlace se anuncia dos veces se conserva el menor costo.
    """
    graph: dict[str, dict[str, int]] = {}
    for origin, record in db.items():
        node = graph.setdefault(origin, {})
        for link in record.links:
            dest, cost = link.get("to"), int(link.get("cost", 1))
            if dest is None:
                continue
            graph.setdefault(dest, {})
            if dest not in node or cost < node[dest]:
                node[dest] = cost
    return graph


def dijkstra(graph: dict[str, dict[str, int]], source: str
             ) -> tuple[dict[str, float], dict[str, str]]:
    """
    Dijkstra clasico con cola de prioridad.
    Devuelve (distancia_minima_por_destino, primer_salto_por_destino).
    El primer salto es el vecino directo de `source` por el que arranca la ruta.
    """
    dist: dict[str, float] = {node: INFINITY for node in graph}
    dist[source] = 0
    first_hop: dict[str, str] = {}
    visited: set[str] = set()
    queue: list[tuple[float, str]] = [(0.0, source)]

    while queue:
        d, node = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)

        for neighbor, cost in sorted(graph.get(node, {}).items()):
            if neighbor in visited:
                continue
            candidate = d + cost
            if candidate < dist.get(neighbor, INFINITY):
                dist[neighbor] = candidate
                # el primer salto se hereda del nodo actual, salvo saliendo del origen
                first_hop[neighbor] = neighbor if node == source else first_hop[node]
                heapq.heappush(queue, (candidate, neighbor))

    return dist, first_hop


@dataclass
class Route:
    destination: str
    next_hop: str
    ip: str
    port: int
    cost: float


def compute_routes(db: dict[str, LsaRecord], source: str,
                   resolve) -> list[Route]:
    """
    Corre Dijkstra y arma la tabla de ruteo.
    `resolve(node_id)` debe devolver un Peer (vecino directo) o None.
    Solo se incluyen destinos alcanzables cuyo siguiente salto sea resoluble.
    """
    graph = build_graph(db)
    graph.setdefault(source, {})
    dist, first_hop = dijkstra(graph, source)

    routes: list[Route] = []
    for destination in sorted(graph):
        if destination == source or dist.get(destination, INFINITY) == INFINITY:
            continue
        hop = first_hop.get(destination)
        peer = resolve(hop) if hop else None
        if peer is None:
            continue  # el siguiente salto no es un vecino directo conocido
        routes.append(Route(destination, hop, peer.ip, peer.port, dist[destination]))
    return routes


# --------------------------------------------------------------------------- #
# persistencia
# --------------------------------------------------------------------------- #
def write_table(routes: list[Route], path: str | Path) -> Path:
    """Escribe nodo_tabla_enrutamiento.csv de forma atomica."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for route in routes:
            cost = int(route.cost) if float(route.cost).is_integer() else route.cost
            writer.writerow([route.destination, route.next_hop, route.ip,
                             route.port, cost])
    tmp.replace(path)
    return path


def read_table(path: str | Path) -> dict[str, Route]:
    """Lee la tabla desde el CSV (usado por el plano de datos)."""
    path = Path(path)
    if not path.exists():
        return {}
    table: dict[str, Route] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            table[row["destino"]] = Route(
                row["destino"], row["siguiente_salto"], row["ip"],
                int(row["puerto"]), float(row["costo"]),
            )
    return table


def format_table(routes: list[Route], source: str) -> str:
    """Version legible de la tabla para imprimir en consola."""
    lines = [f"  tabla de enrutamiento de {source}",
             "  " + "-" * 52,
             f"  {'destino':<10}{'sig.salto':<12}{'ip:puerto':<22}{'costo':>6}"]
    for r in routes:
        cost = int(r.cost) if float(r.cost).is_integer() else r.cost
        lines.append(f"  {r.destination:<10}{r.next_hop:<12}"
                     f"{r.ip + ':' + str(r.port):<22}{cost:>6}")
    if not routes:
        lines.append("  (sin rutas todavia)")
    return "\n".join(lines)
