"""Дельты счётчиков: смена ширины, discontinuity, сбросы и переполнение."""
from __future__ import annotations


def rebooted(previous, current, elapsed):
    if previous is None or current is None or elapsed is None:
        return False
    if current >= previous:
        return False
    # TimeTicks сам переполняется примерно через 497 суток.
    wrapped = (current - previous) % (1 << 32)
    return abs(wrapped / 100 - elapsed) > max(5, elapsed * .1)


def delta(previous, current, bits=32, *, max_delta=None):
    if previous is None or current is None:
        return None
    modulus = 1 << bits
    if not (0 <= previous < modulus and 0 <= current < modulus):
        return None
    if max_delta is not None and max_delta >= modulus:
        # Несколько оборотов Counter32 неотличимы от малого трафика.
        return None
    value = current - previous
    if value < 0:
        if previous < modulus * .75 or current > modulus * .25:
            return None
        value += modulus
    if max_delta is not None and value > max_delta:
        return None
    return value


def rates(previous, current, elapsed, speed_mbps, *, reset=False):
    result = {key: None for key in ("in_bps", "out_bps", "in_error_delta", "out_error_delta",
                                    "in_discard_delta", "out_discard_delta")}
    if reset or not previous or elapsed is None or elapsed <= 0:
        return result
    if previous.get("discontinuity") != current.get("discontinuity"):
        return result
    for direction in ("in", "out"):
        width = current.get(f"{direction}_bits", 32)
        if width == previous.get(f"{direction}_bits", 32):
            bound = speed_mbps * 1e6 * elapsed / 8 * 1.05 if speed_mbps else None
            diff = delta(previous.get(direction), current.get(direction), width, max_delta=bound)
            if diff is not None:
                result[f"{direction}_bps"] = diff * 8 / elapsed
        for counter, output in (("errors", "error"), ("discards", "discard")):
            result[f"{direction}_{output}_delta"] = delta(previous.get(f"{direction}_{counter}"),
                                                          current.get(f"{direction}_{counter}"))
    return result
