"""
Pruebas del PLANO DE CONTROL — responsable: Nicolle

    python3 -m unittest tests.test_control -v
"""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from linkstate import transport                                    # noqa: E402
from linkstate.config import NodeConfig, Peer, parse_peer          # noqa: E402
from linkstate.routing import (LinkStateDB, LsaRecord, build_graph,  # noqa: E402
                               compute_routes, dijkstra)


class TestLSDB(unittest.TestCase):
    def test_solo_acepta_seq_mayor(self):
        db = LinkStateDB()
        self.assertTrue(db.update("A", 1, [{"to": "B", "cost": 2}]))
        self.assertFalse(db.update("A", 1, [{"to": "B", "cost": 2}]))   # repetido
        self.assertFalse(db.update("A", 0, []))                          # viejo
        self.assertTrue(db.update("A", 2, [{"to": "B", "cost": 5}]))     # nuevo
        self.assertEqual(db.snapshot()["A"].links[0]["cost"], 5)


class TestDijkstra(unittest.TestCase):
    """Topologia del enunciado, vista desde F."""

    LINKS = [("A", "B", 2), ("A", "I", 1), ("A", "C", 7), ("B", "F", 1),
             ("F", "D", 1), ("F", "G", 3), ("F", "H", 4), ("I", "D", 6),
             ("C", "D", 5), ("D", "E", 3), ("E", "G", 4)]

    def _db(self):
        adjacency: dict[str, list[dict]] = {}
        for a, b, cost in self.LINKS:
            adjacency.setdefault(a, []).append({"to": b, "cost": cost})
            adjacency.setdefault(b, []).append({"to": a, "cost": cost})
        return {n: LsaRecord(n, 1, links) for n, links in adjacency.items()}

    def test_distancias_desde_F(self):
        dist, hop = dijkstra(build_graph(self._db()), "F")
        self.assertEqual(dist["B"], 1)
        self.assertEqual(dist["D"], 1)
        self.assertEqual(dist["A"], 3)      # F-B-A
        self.assertEqual(dist["G"], 3)
        self.assertEqual(dist["H"], 4)
        self.assertEqual(dist["E"], 4)      # F-D-E
        self.assertEqual(dist["I"], 4)      # F-B-A-I  (mejor que F-D-I = 7)
        self.assertEqual(dist["C"], 6)      # F-D-C
        self.assertEqual(hop["A"], "B")
        self.assertEqual(hop["C"], "D")
        self.assertEqual(hop["I"], "B")

    def test_tabla_solo_incluye_vecinos_resolubles(self):
        peers = {"B": Peer("B", "127.0.0.1", 5002, 1),
                 "D": Peer("D", "127.0.0.1", 5004, 1),
                 "G": Peer("G", "127.0.0.1", 5007, 3),
                 "H": Peer("H", "127.0.0.1", 5008, 4)}
        routes = compute_routes(self._db(), "F", peers.get)
        table = {r.destination: r for r in routes}
        self.assertEqual(set(table), {"A", "B", "C", "D", "E", "G", "H", "I"})
        self.assertEqual(table["A"].next_hop, "B")
        self.assertEqual(table["A"].port, 5002)
        self.assertEqual(table["E"].next_hop, "D")

    def test_ruta_cambia_si_cae_un_enlace(self):
        db = self._db()
        db["F"] = LsaRecord("F", 2, [l for l in db["F"].links if l["to"] != "D"])
        db["D"] = LsaRecord("D", 2, [l for l in db["D"].links if l["to"] != "F"])
        dist, hop = dijkstra(build_graph(db), "F")
        self.assertEqual(hop["E"], "G")     # ahora F-G-E
        self.assertEqual(dist["E"], 7)


class TestTopologiaArbitraria(unittest.TestCase):
    """
    La topologia no esta definida en ningun lado del codigo: cada nodo solo
    conoce sus vecinos y el grafo se arma con los LSA que recibe. Estas pruebas
    generan grafos aleatorios y verifican que el resultado sea correcto para
    cualquiera de ellos.
    """

    @staticmethod
    def _grafo_aleatorio(rng, n_nodos):
        """Grafo conexo aleatorio: primero un arbol de expansion, luego extras."""
        nodos = [f"N{i}" for i in range(n_nodos)]
        aristas = [(nodos[i], nodos[rng.randrange(i)], rng.randint(1, 9))
                   for i in range(1, n_nodos)]
        for _ in range(rng.randint(0, n_nodos)):
            a, b = rng.sample(nodos, 2)
            if not any({a, b} == {x, y} for x, y, _ in aristas):
                aristas.append((a, b, rng.randint(1, 9)))
        return nodos, aristas

    @staticmethod
    def _lsdb(nodos, aristas):
        """Lo que cada nodo terminaria conociendo tras converger el flooding."""
        adyacencia = {n: [] for n in nodos}
        for a, b, costo in aristas:
            adyacencia[a].append({"to": b, "cost": costo})
            adyacencia[b].append({"to": a, "cost": costo})
        return {n: LsaRecord(n, 1, links) for n, links in adyacencia.items()}

    @staticmethod
    def _floyd_warshall(nodos, aristas):
        """Referencia independiente para contrastar las distancias de Dijkstra."""
        INF = float("inf")
        dist = {a: {b: (0 if a == b else INF) for b in nodos} for a in nodos}
        for a, b, costo in aristas:
            dist[a][b] = min(dist[a][b], costo)
            dist[b][a] = min(dist[b][a], costo)
        for k in nodos:
            for i in nodos:
                for j in nodos:
                    if dist[i][k] + dist[k][j] < dist[i][j]:
                        dist[i][j] = dist[i][k] + dist[k][j]
        return dist

    def test_dijkstra_coincide_con_floyd_warshall(self):
        rng = random.Random(2026)
        for _ in range(30):
            nodos, aristas = self._grafo_aleatorio(rng, rng.randint(3, 12))
            grafo = build_graph(self._lsdb(nodos, aristas))
            referencia = self._floyd_warshall(nodos, aristas)
            for origen in nodos:
                dist, _ = dijkstra(grafo, origen)
                for destino in nodos:
                    self.assertEqual(dist[destino], referencia[origen][destino],
                                     f"grafo {aristas}, de {origen} a {destino}")

    def test_siguiente_salto_lleva_al_destino(self):
        """
        Verifica la propiedad que hace funcionar el reenvio salto a salto: si
        cada nodo del camino aplica su propia tabla, el mensaje llega al destino
        y el costo acumulado coincide con el que calculo el nodo origen.
        """
        rng = random.Random(7)
        for _ in range(20):
            nodos, aristas = self._grafo_aleatorio(rng, rng.randint(4, 10))
            db = self._lsdb(nodos, aristas)
            grafo = build_graph(db)
            tablas = {n: {d: h for d, h in dijkstra(grafo, n)[1].items()} for n in nodos}
            distancias = {n: dijkstra(grafo, n)[0] for n in nodos}

            for origen in nodos:
                for destino in nodos:
                    if origen == destino:
                        continue
                    actual, acumulado, saltos = origen, 0, 0
                    while actual != destino:
                        siguiente = tablas[actual][destino]
                        acumulado += grafo[actual][siguiente]
                        actual = siguiente
                        saltos += 1
                        self.assertLessEqual(saltos, len(nodos),
                                             "el reenvio entro en un bucle")
                    self.assertEqual(acumulado, distancias[origen][destino])

    def test_hosts_se_alcanzan_por_su_gateway(self):
        """Un cliente/servidor anunciado con costo 0 aparece como hoja del grafo."""
        nodos, aristas = ["X", "Y", "Z"], [("X", "Y", 3), ("Y", "Z", 2)]
        db = self._lsdb(nodos, aristas)
        db["Z"] = LsaRecord("Z", 2, db["Z"].links + [{"to": "srv", "cost": 0}])
        dist, hop = dijkstra(build_graph(db), "X")
        self.assertEqual(dist["srv"], 5)     # X-Y-Z, el host no agrega costo
        self.assertEqual(hop["srv"], "Y")


class TestDefinicionDeNodo(unittest.TestCase):
    """El nodo se puede definir por archivo, por argumentos o interactivamente."""

    def test_parseo_de_vecinos(self):
        peer = parse_peer("B:100.64.0.2:5000:7")
        self.assertEqual((peer.node_id, peer.ip, peer.port, peer.cost),
                         ("B", "100.64.0.2", 5000, 7))
        self.assertEqual(parse_peer("B:100.64.0.2:5000").cost, 1)   # costo por omision
        for malo in ["B:100.64.0.2", "solotexto", ":100.64.0.2:5000"]:
            with self.assertRaises(ValueError):
                parse_peer(malo)

    def test_definicion_por_argumentos(self):
        config = NodeConfig.from_args(
            "F", 5000, neighbors=["B:127.0.0.1:5002:1", "D:127.0.0.1:5004:1"],
            hosts=["cliente1:127.0.0.1:6001"])
        self.assertEqual([n.node_id for n in config.neighbors], ["B", "D"])
        self.assertEqual(config.peer("cliente1").port, 6001)
        self.assertEqual(config.host_ids, {"cliente1"})

    def test_configuracion_invalida(self):
        with self.assertRaises(ValueError):      # router sin vecinos
            NodeConfig.from_args("F", 5000, neighbors=[])
        with self.assertRaises(ValueError):      # vecino de si mismo
            NodeConfig.from_args("F", 5000, neighbors=["F:127.0.0.1:5000"])
        with self.assertRaises(ValueError):      # vecinos repetidos
            NodeConfig.from_args("F", 5000,
                                 neighbors=["B:127.0.0.1:5002", "B:127.0.0.1:5003"])
        with self.assertRaises(ValueError):      # cliente con dos gateways
            NodeConfig.from_args("c1", 6001, role="client",
                                 neighbors=["A:127.0.0.1:5001", "B:127.0.0.1:5002"])

    def test_guardar_y_recargar(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "F.json"
            original = NodeConfig.from_args(
                "F", 5000, neighbors=["B:127.0.0.1:5002:3"],
                hosts=["cliente1:127.0.0.1:6001"])
            original.save(ruta)
            recargado = NodeConfig.load(ruta)
            self.assertEqual(recargado.to_dict(), original.to_dict())


class TestMensajes(unittest.TestCase):
    def test_campos_del_protocolo(self):
        self.assertEqual(transport.hello("F", "B")["type"], "HELLO")
        packet = transport.lsa("F", 3, [{"to": "B", "cost": 1}], "F")
        self.assertEqual(list(packet), ["type", "origin", "seq", "links", "from"])
        data = transport.message("cliente1", "servidor1", "Hola")
        self.assertEqual(list(data), ["type", "from", "to", "hops", "payload"])

    def test_json_en_una_sola_linea(self):
        self.assertNotIn("\n", transport.dumps(
            transport.message("cliente1", "servidor1", "con ñ y tildes")))

if __name__ == "__main__":
    unittest.main(verbosity=2)
