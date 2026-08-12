"""
Definicion de un nodo: identidad + vecinos directos.

Un nodo NUNCA conoce el grafo completo de la red. Lo unico que se le da al
arrancar es quien es el y con quien esta conectado directamente; el resto de la
topologia la aprende sola por medio de los LSA que le llegan. Por eso la
topologia puede ser cualquiera y no hay ningun grafo cableado en el codigo.

Hay tres formas equivalentes de definir un nodo:

  1. Archivo:      python -m linkstate --config configs/F.json
  2. Argumentos:   python -m linkstate --id F --port 5000 \\
                       --neighbor B:100.64.0.2:5000:1 --neighbor D:100.64.0.4:5000:1
  3. Interactivo:  python -m linkstate            (el nodo pregunta su configuracion)

Formato del archivo:

{
  "node_id": "F",
  "listen_ip": "100.x.x.x",
  "listen_port": 5000,
  "role": "router",                  # router | client | server
  "neighbors": [
    { "node_id": "B", "ip": "100.x.x.x", "port": 5000, "cost": 1 }
  ],
  "hosts": [                         # solo routers-gateway (opcional)
    { "node_id": "cliente1", "ip": "100.x.x.x", "port": 6000 }
  ]
}

Notas:
  - Un nodo con role "client" o "server" declara un unico "neighbor": su router
    gateway. No participa en Link State.
  - "hosts" son los clientes/servidores colgados de un router. El router los
    anuncia en su LSA con costo 0 para que el resto de la red sepa por donde
    alcanzarlos, y les entrega el trafico en JSON plano (sin Hamming).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROLES = {"router", "client", "server"}
DEFAULT_COST = 1
HOST_COST = 0


def parse_peer(spec: str, default_cost: int = DEFAULT_COST) -> "Peer":
    """
    Convierte 'B:100.64.0.2:5000:1' en un Peer.
    El costo es opcional: 'B:100.64.0.2:5000' usa el costo por omision.
    """
    parts = spec.split(":")
    if len(parts) not in (3, 4):
        raise ValueError(
            f"vecino '{spec}' mal escrito; use id:ip:puerto[:costo] "
            f"(ej. B:100.64.0.2:5000:1)"
        )
    node_id, ip, port = parts[0], parts[1], parts[2]
    cost = int(parts[3]) if len(parts) == 4 else default_cost
    if not node_id or not ip:
        raise ValueError(f"vecino '{spec}': el id y la ip no pueden ir vacios")
    return Peer(node_id, ip, int(port), cost)


def _ask(prompt: str, default: str = "", required: bool = False) -> str:
    """Lee una respuesta de consola, con valor por omision opcional."""
    label = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        answer = input(label).strip() or default
        if answer or not required:
            return answer
        print("  este dato es obligatorio")


@dataclass(frozen=True)
class Peer:
    node_id: str
    ip: str
    port: int
    cost: int = DEFAULT_COST

    @property
    def address(self) -> tuple[str, int]:
        return (self.ip, self.port)


@dataclass
class NodeConfig:
    node_id: str
    listen_ip: str
    listen_port: int
    role: str
    neighbors: list[Peer] = field(default_factory=list)
    hosts: list[Peer] = field(default_factory=list)
    path: Path | None = None

    # ------------------------------------------------------------------ #
    # construccion
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | Path) -> "NodeConfig":
        """Define el nodo a partir de su archivo config.json."""
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))

        for key in ("node_id", "listen_ip", "listen_port"):
            if key not in raw:
                raise ValueError(f"{path}: falta el campo obligatorio '{key}'")

        config = cls(
            node_id=raw["node_id"],
            listen_ip=raw["listen_ip"],
            listen_port=int(raw["listen_port"]),
            role=raw.get("role", "router"),
            neighbors=[
                Peer(n["node_id"], n["ip"], int(n["port"]),
                     int(n.get("cost", DEFAULT_COST)))
                for n in raw.get("neighbors", [])
            ],
            hosts=[
                Peer(h["node_id"], h["ip"], int(h["port"]),
                     int(h.get("cost", HOST_COST)))
                for h in raw.get("hosts", [])
            ],
            path=path,
        )
        config.validate(str(path))
        return config

    @classmethod
    def from_args(cls, node_id: str, port: int, role: str = "router",
                  listen_ip: str = "0.0.0.0",
                  neighbors: list[str] | None = None,
                  hosts: list[str] | None = None) -> "NodeConfig":
        """
        Define el nodo desde la linea de comandos, sin archivo.
        Cada vecino se escribe  id:ip:puerto[:costo]  y cada host  id:ip:puerto.
        """
        config = cls(
            node_id=node_id,
            listen_ip=listen_ip,
            listen_port=int(port),
            role=role,
            neighbors=[parse_peer(spec, DEFAULT_COST) for spec in (neighbors or [])],
            hosts=[parse_peer(spec, HOST_COST) for spec in (hosts or [])],
        )
        config.validate("argumentos de linea de comandos")
        return config

    @classmethod
    def prompt(cls) -> "NodeConfig":
        """
        Define el nodo preguntando en consola. Es lo que pide el enunciado:
        'al iniciar un nodo, este solicitara la configuracion y procedera a
        descubrir a sus vecinos'.
        """
        print("=" * 58)
        print(" Configuracion del nodo")
        print(" (Enter acepta el valor entre corchetes)")
        print("=" * 58)

        node_id = _ask("ID de este nodo (ej. F)", required=True)
        role = _ask("Rol [router/client/server]", default="router")
        while role not in ROLES:
            print(f"  rol invalido, use uno de {sorted(ROLES)}")
            role = _ask("Rol [router/client/server]", default="router")

        port = int(_ask("Puerto de escucha", default="5000"))
        listen_ip = _ask("IP de escucha", default="0.0.0.0")

        neighbors: list[Peer] = []
        if role == "router":
            print("\n-- Vecinos directos (Enter en el ID para terminar) --")
            while True:
                peer_id = _ask("  ID del vecino")
                if not peer_id:
                    break
                neighbors.append(Peer(
                    peer_id,
                    _ask(f"  IP de {peer_id}", required=True),
                    int(_ask(f"  Puerto de {peer_id}", default="5000")),
                    int(_ask(f"  Costo del enlace hacia {peer_id}", default="1")),
                ))
                print()
        else:
            print("\n-- Router gateway --")
            gateway_id = _ask("  ID del router gateway", required=True)
            neighbors.append(Peer(
                gateway_id,
                _ask(f"  IP de {gateway_id}", required=True),
                int(_ask(f"  Puerto de {gateway_id}", default="5000")),
                0,
            ))

        hosts: list[Peer] = []
        if role == "router":
            print("\n-- Clientes/servidores conectados a este router "
                  "(Enter en el ID para terminar) --")
            while True:
                host_id = _ask("  ID del host")
                if not host_id:
                    break
                hosts.append(Peer(
                    host_id,
                    _ask(f"  IP de {host_id}", required=True),
                    int(_ask(f"  Puerto de {host_id}", default="6000")),
                    HOST_COST,
                ))
                print()

        config = cls(node_id, listen_ip, port, role, neighbors, hosts)
        config.validate("configuracion interactiva")

        destination = _ask("\nGuardar esta configuracion en (Enter para no guardar)")
        if destination:
            config.save(destination)
            print(f"  guardado en {destination}")
        return config

    # ------------------------------------------------------------------ #
    # validacion y persistencia
    # ------------------------------------------------------------------ #
    def validate(self, origin: str = "config") -> None:
        if self.role not in ROLES:
            raise ValueError(f"{origin}: role '{self.role}' invalido, "
                             f"use uno de {sorted(ROLES)}")

        if self.role in ("client", "server") and len(self.neighbors) != 1:
            raise ValueError(
                f"{origin}: un nodo '{self.role}' debe declarar exactamente un "
                f"vecino (su router gateway); se encontraron {len(self.neighbors)}"
            )
        if self.role == "router" and not self.neighbors:
            raise ValueError(f"{origin}: un router necesita al menos un vecino")

        ids = [n.node_id for n in self.neighbors] + [h.node_id for h in self.hosts]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{origin}: hay node_id repetidos entre neighbors/hosts")
        if self.node_id in ids:
            raise ValueError(f"{origin}: el nodo se declara a si mismo como vecino")
        if not (0 < self.listen_port < 65536):
            raise ValueError(f"{origin}: puerto {self.listen_port} fuera de rango")

    def to_dict(self) -> dict:
        data = {
            "node_id": self.node_id,
            "listen_ip": self.listen_ip,
            "listen_port": self.listen_port,
            "role": self.role,
            "neighbors": [
                {"node_id": n.node_id, "ip": n.ip, "port": n.port, "cost": n.cost}
                for n in self.neighbors
            ],
        }
        if self.hosts:
            data["hosts"] = [
                {"node_id": h.node_id, "ip": h.ip, "port": h.port} for h in self.hosts
            ]
        return data

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        self.path = path
        return path

    # ------------------------------------------------------------------ #
    @property
    def gateway(self) -> Peer:
        """Router gateway de un nodo client/server."""
        return self.neighbors[0]

    def peer(self, node_id: str) -> Peer | None:
        """
        Busca un vecino o host directo por node_id.

        Como respaldo tambien acepta que lo identifiquen por su IP: el ejemplo de
        LSA del enunciado usa la IP como 'origin', asi que otra pareja podria
        estar nombrando los nodos por IP en lugar de por letra. Esto salva ese
        caso en una direccion, pero lo correcto es que las tres parejas usen los
        mismos identificadores.
        """
        if not node_id:
            return None
        for p in (*self.neighbors, *self.hosts):
            if p.node_id == node_id:
                return p
        for p in (*self.neighbors, *self.hosts):
            if p.ip == node_id or f"{p.ip}:{p.port}" == node_id:
                return p
        return None

    @property
    def host_ids(self) -> set[str]:
        return {h.node_id for h in self.hosts}
