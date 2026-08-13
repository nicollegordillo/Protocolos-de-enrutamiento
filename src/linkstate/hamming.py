from __future__ import annotations

NIBBLE_BITS = 4
CODE_BITS = 7


def _validate_nibble(nibble: str) -> None:
    if len(nibble) != NIBBLE_BITS or any(b not in "01" for b in nibble):
        raise ValueError(f"nibble invalido: '{nibble}' (se esperaban 4 bits)")


def encode_nibble(nibble: str) -> str:
    _validate_nibble(nibble)
    d1, d2, d3, d4 = (int(b) for b in nibble)

    p1 = d1 ^ d2 ^ d4
    p2 = d1 ^ d3 ^ d4
    p4 = d2 ^ d3 ^ d4

    return f"{p1}{p2}{d1}{p4}{d2}{d3}{d4}"


def decode_nibble(code: str) -> tuple[str, bool]:
    if len(code) != CODE_BITS or any(b not in "01" for b in code):
        raise ValueError(f"codigo invalido: '{code}' (se esperaban 7 bits)")

    bits = [0] + [int(b) for b in code]

    s1 = bits[1] ^ bits[3] ^ bits[5] ^ bits[7]
    s2 = bits[2] ^ bits[3] ^ bits[6] ^ bits[7]
    s4 = bits[4] ^ bits[5] ^ bits[6] ^ bits[7]
    syndrome = s1 + (s2 << 1) + (s4 << 2)

    corrected = syndrome != 0
    if corrected:
        bits[syndrome] ^= 1

    data = f"{bits[3]}{bits[5]}{bits[6]}{bits[7]}"
    return data, corrected


def encode(bits: str) -> str:
    if any(b not in "01" for b in bits):
        raise ValueError("la cadena de entrada debe contener solo '0' y '1'")
    padding = (-len(bits)) % NIBBLE_BITS
    padded = bits + "0" * padding
    return "".join(
        encode_nibble(padded[i:i + NIBBLE_BITS])
        for i in range(0, len(padded), NIBBLE_BITS)
    )


def decode(encoded: str) -> tuple[str, bool]:
    if len(encoded) % CODE_BITS != 0:
        raise ValueError(
            f"la cadena codificada debe ser multiplo de {CODE_BITS} bits "
            f"(largo recibido: {len(encoded)})"
        )
    data_chunks = []
    any_corrected = False
    for i in range(0, len(encoded), CODE_BITS):
        nibble, corrected = decode_nibble(encoded[i:i + CODE_BITS])
        data_chunks.append(nibble)
        any_corrected = any_corrected or corrected
    return "".join(data_chunks), any_corrected
