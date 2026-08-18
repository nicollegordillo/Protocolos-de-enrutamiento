from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from . import hamming

logger = logging.getLogger("dataplane")

MAX_HOPS = 32


@dataclass
class DataPacket:
    from_id: str
    to_id: str
    payload: str
    hops: int = 0
    type: str = "MESSAGE"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "from": self.from_id,
            "to": self.to_id,
            "hops": self.hops,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DataPacket":
        return cls(
            from_id=data["from"],
            to_id=data["to"],
            payload=data["payload"],
            hops=int(data.get("hops", 0)),
            type=data.get("type", "MESSAGE"),
        )


def _text_to_bits(text: str) -> str:
    return "".join(format(byte, "08b") for byte in text.encode("utf-8"))


def _bits_to_text(bits: str) -> str:
    if len(bits) % 8 != 0:
        raise ValueError(
            f"cantidad de bits de datos ({len(bits)}) no es multiplo de 8; "
            "revisar si el otro extremo esta agregando padding no acordado"
        )
    byte_values = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    return byte_values.decode("utf-8")


def encode_packet(packet: DataPacket) -> str:
    text = json.dumps(packet.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return hamming.encode(_text_to_bits(text))


def decode_packet(bits: str) -> tuple[DataPacket, bool]:

    raw, corrected = hamming.decode(bits)
    text = _bits_to_text(raw)
    packet = DataPacket.from_dict(json.loads(text))
    return packet, corrected


def is_data_frame(line: str) -> bool:
    return bool(line) and all(ch in "01" for ch in line)


CAMPOS_SENSIBLES = {"pin", "password", "clave", "contrasena", "contraseña"}


def resumen_payload(payload, limite: int = 70) -> str:
    """
    Version corta del payload para la traza, ocultando datos sensibles.
    Un router intermedio no tiene por que dejar el PIN escrito en su consola.
    """
    def enmascarar(valor):
        if isinstance(valor, dict):
            return {k: ("***" if k.lower() in CAMPOS_SENSIBLES else enmascarar(v))
                    for k, v in valor.items()}
        if isinstance(valor, list):
            return [enmascarar(v) for v in valor]
        return valor

    texto = json.dumps(enmascarar(payload), ensure_ascii=False)
    return texto if len(texto) <= limite else texto[:limite - 3] + "..."



class Forwarding:


    def __init__(self, node):
        self.node = node
        self.cfg = node.cfg
        self.log = node.log
        self.control = node.control 

    def originate(self, from_id: str, to_id: str, payload: str,
                  addr: tuple | None = None) -> bool:
        # Un MESSAGE en JSON plano solo puede venir de un host propio: es el
        # tramo cliente/servidor <-> gateway, que por acuerdo no lleva Hamming.
        if from_id in self.cfg.host_ids:
            origen = f"{from_id} (host local)"
        else:
            origen = self._quien_es(addr, from_id)
        return self._forward(DataPacket(from_id, to_id, payload, hops=0), origen=origen)

    def _quien_es(self, addr: tuple | None, fallback: str = "?") -> str:
        """
        Identifica de quien llego la trama usando la IP de la conexion entrante.
        El protocolo no lleva un campo con el salto anterior, asi que se resuelve
        contra la lista de vecinos y hosts propios.
        """
        if addr is None:
            return fallback
        coincidencias = [p for p in (*self.cfg.neighbors, *self.cfg.hosts)
                         if p.ip == addr[0]]
        if len(coincidencias) == 1:
            return f"{coincidencias[0].node_id} ({addr[0]})"
        if len(coincidencias) > 1:
            # varios nodos en la misma maquina: la IP de origen no basta para
            # distinguirlos, porque el puerto de salida es efimero
            nombres = "/".join(p.node_id for p in coincidencias)
            return f"{addr[0]} (alguno de: {nombres})"
        return addr[0]

    def handle_frame(self, bits: str, addr: tuple) -> None:
        try:
            packet, corrected = decode_packet(bits)
        except Exception as exc:
            self.log(f"trama de datos descartada, no se pudo decodificar ({addr}): {exc}")
            return
        origen = self._quien_es(addr)
        if corrected:
            self.log(f"Hamming corrigio 1 bit en mensaje {packet.from_id}->{packet.to_id}")

        if packet.to_id == self.cfg.node_id or packet.to_id in self.cfg.host_ids:
            self._deliver(packet, origen)
            return

        if packet.hops >= MAX_HOPS:
            self.log(f"mensaje {packet.from_id}->{packet.to_id} descartado: excede hops")
            return

        self._forward(packet, origen)

    def traza(self, packet: DataPacket, origen: str, route=None,
              bits: int = 0, destino_local: bool = False) -> None:
        """
        Deja constancia del paso del mensaje por este nodo: de quien lo recibio
        y a quien se lo entrega. Es la vista que permite seguir un mensaje salto
        a salto a lo largo de toda la red.
        """
        cabecera = (f"MENSAJE {packet.from_id} -> {packet.to_id}  "
                    f"(salto {packet.hops})")
        if destino_local:
            salida = (f"ENTREGA LOCAL a {packet.to_id}"
                      if packet.to_id in self.cfg.host_ids
                      else "ENTREGA LOCAL (soy el destino)")
        else:
            salida = (f"reenvio a {route.next_hop} "
                      f"[{route.ip}:{route.port}]  {bits} bits Hamming")

        self.log(f"{cabecera}\n"
                 f"          recibido de : {origen}\n"
                 f"          {salida}\n"
                 f"          contenido   : {resumen_payload(packet.payload)}")

    def _deliver(self, packet: DataPacket, origen: str = "?") -> None:
        self.traza(packet, origen, destino_local=True)
        host = self.cfg.peer(packet.to_id) if packet.to_id in self.cfg.host_ids else None
        if host is None:
            return 
        from . import transport
        try:
            transport.send_json(host.ip, host.port, packet.to_dict())
        except OSError as exc:
            self.log(f"no se pudo entregar a host local {packet.to_id}: {exc}")

    def _forward(self, packet: DataPacket, origen: str = "?") -> bool:
        # Destino local: se entrega en JSON plano, SIN Hamming. Este chequeo es
        # indispensable aqui y no solo en handle_frame, porque un MESSAGE que
        # llega en JSON desde un cliente propio entra por originate(); sin el,
        # un cliente y un servidor colgados del mismo router terminarian
        # recibiendo una trama de bits que no saben interpretar.
        if packet.to_id == self.cfg.node_id or packet.to_id in self.cfg.host_ids:
            self._deliver(packet, origen)
            return True

        route = self.control.lookup(packet.to_id)
        if route is None:
            self.log(f"sin ruta hacia {packet.to_id}, mensaje descartado")
            return False

        outgoing = DataPacket(packet.from_id, packet.to_id, packet.payload,
                            hops=packet.hops + 1)
        try:
            frame = encode_packet(outgoing)
            from . import transport
            transport.send_line(route.ip, route.port, frame)
            self.traza(outgoing, origen, route=route, bits=len(frame))
            return True
        except OSError as exc:
            self.log(f"no se pudo reenviar hacia {packet.to_id} via "
                     f"{route.ip}:{route.port}: {exc}")
            return False


def dispatch_line(line: str, addr: tuple, *, forwarding: Forwarding, control) -> None:
    if is_data_frame(line):
        forwarding.handle_frame(line, addr)
        return
    try:
        msg = json.loads(line)
    except ValueError:
        forwarding.log(f"linea descartada, no es bits ni JSON valido ({addr}): {line[:60]!r}")
        return
    control.handle(msg)