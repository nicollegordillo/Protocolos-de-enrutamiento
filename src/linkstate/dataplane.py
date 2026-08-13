
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import hamming

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



class Forwarding:

    def __init__(self, node):
        self.node = node
        self.cfg = node.cfg
        self.log = node.log
        self.control = node.control

    def originate(self, from_id: str, to_id: str, payload: str) -> bool:
        return self._forward(DataPacket(from_id, to_id, payload, hops=0))

    def handle_frame(self, bits: str, addr: tuple) -> None:
        try:
            packet, corrected = decode_packet(bits)
        except Exception as exc:
            self.log(f"trama de datos descartada, no se pudo decodificar ({addr}): {exc}")
            return
        if corrected:
            self.log(f"Hamming corrigio 1 bit en mensaje {packet.from_id}->{packet.to_id}")

        if packet.to_id == self.cfg.node_id or packet.to_id in self.cfg.host_ids:
            self._deliver(packet)
            return

        if packet.hops >= MAX_HOPS:
            self.log(f"mensaje {packet.from_id}->{packet.to_id} descartado: excede MAX_HOPS")
            return

        self._forward(packet)

    def _deliver(self, packet: DataPacket) -> None:
        self.log(f"mensaje entregado: {packet.from_id} -> {packet.to_id}: {packet.payload!r}")
        host = self.cfg.peer(packet.to_id) if packet.to_id in self.cfg.host_ids else None
        if host is None:
            return
        from . import transport
        try:
            transport.send_json(host.ip, host.port, packet.to_dict())
        except OSError as exc:
            self.log(f"no se pudo entregar a host local {packet.to_id}: {exc}")

    def _forward(self, packet: DataPacket) -> bool:
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
