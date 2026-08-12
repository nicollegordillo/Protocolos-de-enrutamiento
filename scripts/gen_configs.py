#!/usr/bin/env python3
"""
UTILIDAD OPCIONAL, solo para armar pruebas locales rapido.

Genera de un jalon un config.json por nodo a partir de un archivo que describe
una topologia completa. NO es parte del protocolo: los nodos nunca leen este
archivo y ninguno conoce el grafo de la red. Sirve unicamente para no escribir
a mano once configuraciones cuando uno quiere probar una topologia en su propia
maquina.

    python scripts/gen_configs.py
    python scripts/gen_configs.py --topology mi_topologia.json --out configs

Un nodo se puede definir igual de bien sin este script:

    python -m linkstate --id F --port 5000 -n B:100.64.0.2:5000:1
    python -m linkstate                      # modo interactivo
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def generate(topology: dict) -> dict[str, dict]:
    nodes = topology["nodes"]
    links = topology["links"]

    adjacency: dict[str, list[dict]] = {n: [] for n in nodes}
    hosts: dict[str, list[dict]] = {n: [] for n in nodes}

    # enlaces entre routers (bidireccionales)
    for a, b, cost in links:
        for src, dst in ((a, b), (b, a)):
            if src not in nodes or dst not in nodes:
                raise ValueError(f"el enlace {a}-{b} referencia un nodo inexistente")
            adjacency[src].append({
                "node_id": dst,
                "ip": nodes[dst]["ip"],
                "port": nodes[dst]["port"],
                "cost": int(cost),
            })

    # clientes/servidores colgados de su router gateway
    for node_id, spec in nodes.items():
        if spec["role"] == "router":
            continue
        gateway = spec["gateway"]
        adjacency[node_id].append({
            "node_id": gateway,
            "ip": nodes[gateway]["ip"],
            "port": nodes[gateway]["port"],
            "cost": 0,
        })
        hosts[gateway].append({
            "node_id": node_id,
            "ip": spec["ip"],
            "port": spec["port"],
        })

    configs = {}
    for node_id, spec in nodes.items():
        config = {
            "node_id": node_id,
            "listen_ip": "0.0.0.0" if spec.get("bind_any", True) else spec["ip"],
            "listen_port": spec["port"],
            "role": spec["role"],
            "neighbors": sorted(adjacency[node_id], key=lambda n: n["node_id"]),
        }
        if hosts[node_id]:
            config["hosts"] = sorted(hosts[node_id], key=lambda h: h["node_id"])
        configs[node_id] = config
    return configs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", default="scripts/topologia_ejemplo.json")
    parser.add_argument("--out", default="configs")
    args = parser.parse_args()

    topology = json.loads(Path(args.topology).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for node_id, config in generate(topology).items():
        path = out_dir / f"{node_id}.json"
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        print(f"  {path}  ({config['role']}, "
              f"{len(config['neighbors'])} vecino(s))")


if __name__ == "__main__":
    main()
