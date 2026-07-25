"""Settings-sync gate: model ranges, FNV hash, upload decision logic.

Pure module — no HA imports, no I/O.
"""

# Config register ranges: model family → (start, count)
# 3-phase and 1-phase models share the same register count (63 each)
CONFIG_RANGES = {
    # 3-phase models: registers 115-177 (63 registers)
    'deye_sg04lp3': (115, 63),
    'deye_sg01hp3': (115, 63),
    'deye_sg01hp3_50k': (115, 63),
    'deye_sg01hp3_30k': (115, 63),
    'deye_sg02hp3_80k': (115, 63),
    'deye_sg05lp3': (115, 63),
    'deye_gb_s20k': (115, 63),
    'sunsynk_3phase': (115, 63),
    'sunsynk_3phase_15k': (115, 63),
    # 1-phase models: registers 217-279 (63 registers)
    'deye_sg03lp1': (217, 63),
    'deye_sg01lp1': (217, 63),
    'deye_sg02lp1': (217, 63),
    'deye_sun_g3': (217, 63),
    'deye_sg04lp1': (217, 63),
    'deye_sg05lp1': (217, 63),
    'deye_sg01lp1_16k': (217, 63),
    'sunsynk_1phase': (217, 63),
    'sunsynk_1phase_12k': (217, 63),
    'sunsynk_1phase_16k': (217, 63),
    'deye_sg01lp1_us': (217, 63),
    'solark_5k': (217, 63),
    'solark_12k': (217, 63),
    'solark_15k': (217, 63),
    'solark_18k': (217, 63),
}


def config_range_for_model(model_id: str) -> tuple[int, int] | None:
    """Return (start, count) register range for a model, or None if unsupported."""
    return CONFIG_RANGES.get(model_id)


def registers_hash(regs: list[int]) -> int:
    """FNV-1a hash of register list (byte-folded).

    Identical to firmware `settings_registers_hash` and server algorithm.
    Each 16-bit register is folded into low byte then high byte.
    """
    h = 2166136261
    for v in regs:
        # Low byte
        h = ((h ^ (v & 0xFF)) * 16777619) & 0xFFFFFFFF
        # High byte
        h = ((h ^ ((v >> 8) & 0xFF)) * 16777619) & 0xFFFFFFFF
    return h


def should_upload(
    new_hash: int,
    cached_hash: int,
    last_uploaded_monotonic: int,
    now_monotonic: int,
    heartbeat_s: int = 1800,
) -> bool:
    """Determine if config registers should be uploaded to the server.

    Four clauses (OR):
    1. Bootstrap: cached_hash == 0 (first poll, no cache)
    2. Changed: new_hash != cached_hash (register state changed)
    3. Fresh + unchanged: last_uploaded_monotonic == 0 (never uploaded, even if unchanged)
    4. Heartbeat: now_monotonic >= last_uploaded_monotonic + heartbeat_s (periodic refresh)
    """
    return (
        cached_hash == 0  # Bootstrap
        or new_hash != cached_hash  # Changed
        or last_uploaded_monotonic == 0  # Fresh (never uploaded)
        or now_monotonic >= last_uploaded_monotonic + heartbeat_s  # Heartbeat
    )
