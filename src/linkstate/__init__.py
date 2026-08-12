"""
Laboratorio 3 - Protocolos de enrutamiento (Link State)
Universidad del Valle de Guatemala - CC3067 Redes

Modulos:
    config      carga de config.json
    transport   framing por lineas sobre TCP + constructores de mensajes
    hamming     codec Hamming(7,4)
    routing     LSDB, Dijkstra y nodo_tabla_enrutamiento.csv
    control     plano de control: HELLO, LSA, flooding, Dijkstra
    datos       plano de datos: Hamming, consulta de tabla, reenvio
    router      arma el nodo y corre los hilos
    endpoint    nodo cliente / servidor
"""

__version__ = "1.0.0"
