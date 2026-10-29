/**
 * Structural subset of `InverterReadingSchema` (services/api/src/models/schemas.ts)
 * that `validateReading` actually reads/writes. Deliberately NOT importing the
 * concrete `InverterReading` type from services/api — that schema file is
 * entangled with unrelated calendar/action-catalog modules and importing it here
 * would create a backward dependency (packages/reading-core → services/api).
 * `validateReading` is generic over `T extends ReadingLike` so callers passing
 * the real `InverterReading` (or any structurally-compatible object) get their
 * concrete type back unchanged in `sanitizedData` — see the function below.
 */
import { batteryReadingContradictsSoc, configuredPackFloorV } from './battery-frame-corruption.js';

export interface ReadingLike {
  timestamp: string;
  pvPower: number;
  pvPower1?: number;
  pvPower2?: number;
  pvPower3?: number;
  pvPower4?: number;
  pvPower5?: number;
  pvPower6?: number;
  batteryPower: number;
  batterySoc?: number;
  batteryVoltage: number;
  batteryPower2?: number;
  gridPower: number;
  loadPower?: number;
  gridVoltage?: number;
  phaseVoltages?: number[];
  phaseGridPowers?: number[];
  phaseGridCurrents?: number[];
  dailyPvEnergy?: number;
  dailyImportEnergy?: number;
  dailyExportEnergy?: number;
  dailyLoadEnergy?: number;
  generatorPower?: number;
  generatorVoltage?: number;
  generatorFrequency?: number;
  genPortMode?: number;
}

/**
 * Structural per-field power-scale calibration factors used by
 * `applyPowerScaleOverrides` (packages/reading-core/src/power-scale.ts). In
 * practice each factor is restricted to {0.1, 1, 10} by services/api's
 * `PowerScaleOverridesSchema`, which validates admin input before it ever
 * reaches this package — but this type is deliberately a plain structural
 * shape (not `z.infer<typeof PowerScaleOverridesSchema>`) so
 * packages/reading-core never imports from services/api.
 */
export interface PowerScaleOverrides {
  grid?: number;
  pv?: number;
  load?: number;
  battery?: number;
  energy?: number;
  /**
   * Battery CURRENT (reg 191 on the Deye/Sunsynk 1-phase map), scaled
   * independently of `battery` because the two registers fail apart: some
   * SG02LP1 / SG01LP1-16K units report current in 0.1 A units while the map
   * declares 0.01 A, and their battery POWER register is correct. See #141.
   */
  batteryCurrent?: number;
}

/**
 * Model-specific power limits derived from rated inverter power.
 * Mirrors InverterModel from packages/inverter_protocol.
 */
interface ModelLimits {
  ratedPowerW: number;
  nominalAcPowerW: number;        // AC output rating — PV above this is "bonus energy"
  maxPvPowerW: number;            // ratedPower * 2 (DC input can exceed AC rating)
  maxGridPowerW: number;          // ratedPower * 1.5
  maxLoadPowerW: number;          // ratedPower * 1.5
  maxBatteryPowerW: number;       // ratedPower * 1.2
  maxDailyEnergyKwh: number;      // ratedPower/1000 * 10 (peak sun hours) — PV only
  maxDailyGridEnergyKwh: number;  // flat 500 for all models — grid import/export/load/battery
  /** `pvPower` comes from the inverter's own DC-input register, never from a
   *  sum of the per-string slots. Mirrors `pvPowerRegisterIsAggregate` on the
   *  Dart register map. The phantom-string clamp reads it to leave such a
   *  total whole (issue #622). */
  pvTotalIsDcInputRegister?: boolean;
}

function limitsFromRated(ratedPowerW: number): ModelLimits {
  return {
    ratedPowerW,
    nominalAcPowerW: ratedPowerW,
    maxPvPowerW: ratedPowerW * 2,
    maxGridPowerW: Math.round(ratedPowerW * 1.5),
    maxLoadPowerW: Math.round(ratedPowerW * 1.5),
    maxBatteryPowerW: Math.round(ratedPowerW * 1.2),
    maxDailyEnergyKwh: (ratedPowerW / 1000) * 10,
    // Grid-side daily is bounded by a 24h-continuous connection, not sun hours.
    // Households with electric heating legitimately hit 300-400 kWh/day.
    // Bogus cemented-counter values are in the thousands; 500 catches them
    // while never clipping a real home load.
    maxDailyGridEnergyKwh: 500,
  };
}

const MODEL_LIMITS: Record<string, ModelLimits> = {
  // Deye 3-Phase
  deye_sg04lp3: limitsFromRated(12000),
  deye_sg03lp1: limitsFromRated(8000),
  deye_sg01hp3: limitsFromRated(25000),
  deye_sg01hp3_50k: limitsFromRated(50000),
  deye_sg01hp3_30k: limitsFromRated(30000),
  deye_sg02hp3_80k: limitsFromRated(80000),
  deye_sg05lp3: limitsFromRated(20000),
  deye_gb_s20k: limitsFromRated(20000),
  // Deye 1-Phase
  deye_sun_g3: limitsFromRated(6000),
  deye_sg01lp1: limitsFromRated(8000),
  deye_sg02lp1: limitsFromRated(12000),
  deye_sg01lp1_us: limitsFromRated(8000),
  deye_sg04lp1: limitsFromRated(6000),
  deye_sg05lp1: limitsFromRated(8000),
  deye_sg01lp1_16k: limitsFromRated(16000),
  // Anenji. TWO platforms share this brand, and they are not related:
  //   - the 12 kW split-phase is an SRNE ASF-HF rebadge (`srne_anenji_12k`)
  //   - everything else is an EASUN/ISolar SMG II clone, read over its own
  //     SmartESS/EyBond collector
  // Only the ratings matter here, but the note is worth keeping: picking
  // anything by the brand name is how the two got conflated in the first place.
  // ANJ-4.2KW-24V-W-Pro. From the NAMEPLATE of the bench unit (serial
  // 99432604107106, read 2026-08-21), not from the model name -- the
  // third-party catalogue that named an "ANJ-4000W-24V" was wrong about the
  // rating, and 4.2 kW is what the label says.
  //
  // maxGridPowerW is overridden because `limitsFromRated` derives it as 1.5x
  // rated, and that is WRONG for this class of hybrid: the AC charger draws
  // 230 V x 32.4 A = 7452 W, running a 4.2 kW load while pushing 120 A into a
  // 24 V pack. 1.5x rated is 6300 W, so a real full-draw reading would have
  // been rejected -- and rejection is silent, showing the user a gap in the
  // chart rather than an error.
  anenji_anj_4200: {
    ...limitsFromRated(4200),
    maxGridPowerW: 7500,
  },
  anenji_anj_5kw: limitsFromRated(5000),
  anenji_anj_6200: limitsFromRated(6200),
  anenji_anj_11kw: limitsFromRated(11000),
  anenji_anj_12kw: limitsFromRated(12000),
  // Deye Grid-Tie
  deye_string: limitsFromRated(15000),
  deye_micro: limitsFromRated(2000),
  // Grid-tie string inverters (no battery)
  deye_sun_60k_g03: limitsFromRated(60000),
  solplanet_asw100k_lt: limitsFromRated(100000),
  solplanet_asw110k_lt: limitsFromRated(110000),
  // Solis (Ginlong) 5G 3-phase string, no battery (issue #118). Catalogue
  // ratedPowerW is 30000 — NOT added to HV_BATTERY_MODELS, this model has
  // no battery pack at all.
  solis_30k_5g: limitsFromRated(30000),
  // Solis hybrids (ESINV-33000ID), 2026-10-26. Ratings are each series'
  // highest variant from the datasheets; nothing measured
  // (docs/inverter-research/2026-10-26-solis-hybrid.md, Phase 2). The four
  // 48 V entries are NOT in HV_BATTERY_MODELS; solis_s6_eh3p_h (120-800 V) is.
  solis_s6_eh1p_l_plus: limitsFromRated(8000),
  solis_s6_eh1p_10k_l_plus: limitsFromRated(10000),
  solis_rhi_48es_5g: limitsFromRated(6000),
  solis_s6_eh3p_nv_yd_l: limitsFromRated(15000),
  solis_s6_eh3p_h: limitsFromRated(20000),
  // Sunsynk
  sunsynk_1phase: limitsFromRated(8000),
  sunsynk_3phase: limitsFromRated(12000),
  sunsynk_1phase_12k: limitsFromRated(12000),
  sunsynk_1phase_16k: limitsFromRated(16000),
  sunsynk_3phase_15k: limitsFromRated(15000),
  // Sol-Ark
  solark_5k: limitsFromRated(5000),
  solark_12k: limitsFromRated(12000),
  solark_15k: limitsFromRated(15000),
  solark_18k: limitsFromRated(18000),
  // Victron
  victron_multiplus_ii_gx_6k5: limitsFromRated(6500),
  victron_multiplus_ii_3k: limitsFromRated(3000),
  victron_multiplus_ii_5k: limitsFromRated(5000),
  victron_multiplus_ii_10k: limitsFromRated(10000),
  victron_multiplus_ii_15k: limitsFromRated(15000),
  victron_multiplus_ii_4k: limitsFromRated(4000),
  victron_multiplus_ii_8k: limitsFromRated(8000),
  victron_quattro_ii_5k: limitsFromRated(5000),
  // Huawei SUN2000 (commercial string, no battery). `pvPower` is register
  // 32064, the inverter's DC input; the per-slot powers are one MPPT each.
  huawei_sun2000_30ktl_m3: { ...limitsFromRated(30000), pvTotalIsDcInputRegister: true },
  huawei_sun2000_50ktl_m3: { ...limitsFromRated(50000), pvTotalIsDcInputRegister: true },
  huawei_sun2000_150k_mg0: { ...limitsFromRated(150000), pvTotalIsDcInputRegister: true },
  // Generic catch-all for any SUN2000 whose exact rating is unknown.
  // Uses the most permissive Huawei entry so an unidentified large unit is never
  // false-clamped, while 0xFFFF-scale garbage (>300 kW) is still caught.
  huawei_sun2000: { ...limitsFromRated(150000), pvTotalIsDcInputRegister: true },
  // Sofar HYD 5-20KTL-3PH (Wave A brand-expansion). Split into two entries
  // (fix round 1) so ratedPowerW slack stays within 2x for the smallest
  // unit — a single 5-20 kW entry at 20000 gave a real 5 kW owner ceilings
  // sized for a 20 kW unit. High-voltage pack — BOTH are in HV_BATTERY_MODELS.
  sofar_hyd_3ph_10k: limitsFromRated(10000),
  sofar_hyd_3ph_20k: limitsFromRated(20000),
  // SRNE ASF/HF/HESP/HSI LV hybrids (Wave A brand-expansion batch 2). One
  // register map, five nameplate bands — the range is 1.5-12 kW (8x), so it
  // cannot be one entry under the 2x rule. All are 48V-or-lower LV packs
  // (`srne_hsi_1500` is 12V) — NOT added to HV_BATTERY_MODELS.
  srne_hsi_1500: limitsFromRated(1500),
  srne_hf_3k6: limitsFromRated(3600),
  srne_hf_6k5: limitsFromRated(6500),
  srne_asf_10k: limitsFromRated(10000),
  srne_anenji_12k: limitsFromRated(12000),
  // Megarevo R…KH3 three-phase hybrids (Wave A brand-expansion batch 3). The
  // H3 family is 6-15 kW = 2.5x, so it cannot be one entry under the 2x rule.
  // The upper band is rated 16500 (NOT 15000): source 1's own header says the
  // family runs to 16.5 kW, and a 15000 ceiling would clamp a genuine reading.
  // HV battery packs (125-600 V) — BOTH are in HV_BATTERY_MODELS below.
  megarevo_r_h3_10k: limitsFromRated(10000),
  megarevo_r_h3_15k: limitsFromRated(16500),
  // KSTAR KSE / BluE-S LV hybrids + the Eaton xStorage Hybrid rebrand (Wave A
  // brand-expansion batch 3). Bands come from the vendor PDF's own Table 3.1.5:
  // the single-phase family runs 2-6 kW = 3.0x and must split; the three-phase
  // family is 8-12 kW = 1.5x and needs one entry. All 48 V LV — KSTAR has no
  // HV hybrid line, so NONE is in HV_BATTERY_MODELS.
  kstar_hybrid_1p_3k: limitsFromRated(3680),
  kstar_hybrid_1p_6k: limitsFromRated(6000),
  kstar_hybrid_3p_12k: limitsFromRated(12000),
  // Afore (Wave A brand-expansion batch 3; BANDED in fix round 2). The first
  // cut shipped one entry per family at the family TOP — 30 000 W over a
  // 3-30 kW hybrid line (10x) and 110 000 W over a 3-110 kW BNT line (36.7x) —
  // citing the brief's "err HIGH" ruling. That ruling picks an end WITHIN a
  // ≤2x band; it does not authorise collapsing 36.7x. At 110 kW a 4 kW BNT got
  // maxPvPowerW 220 kW and maxDailyEnergyKwh 1100, i.e. no defence at all.
  //
  // Bands double from each family's floor, so no band spans more than 2.00x:
  // hybrid 3-6/6-12/12-24/24-30, BNT 3-6/6-12/12-24/24-48/48-96/96-110. Within
  // a band the entry still carries the top nameplate — that is where "err HIGH"
  // legitimately applies. The four `afore_hybrid_*` bands are the AF…K-TH
  // HV-battery line and are ALL in HV_BATTERY_MODELS below; the twelve BNT
  // entries are grid-tie strings with no battery at all.
  afore_hybrid_6k: limitsFromRated(6000),
  afore_hybrid_12k: limitsFromRated(12000),
  afore_hybrid_24k: limitsFromRated(24000),
  afore_hybrid_30k: limitsFromRated(30000),
  afore_bnt_t4_6k: limitsFromRated(6000),
  afore_bnt_t4_12k: limitsFromRated(12000),
  afore_bnt_t4_24k: limitsFromRated(24000),
  afore_bnt_t4_48k: limitsFromRated(48000),
  afore_bnt_t4_96k: limitsFromRated(96000),
  afore_bnt_t4_110k: limitsFromRated(110000),
  afore_bnt_t6_6k: limitsFromRated(6000),
  afore_bnt_t6_12k: limitsFromRated(12000),
  afore_bnt_t6_24k: limitsFromRated(24000),
  afore_bnt_t6_48k: limitsFromRated(48000),
  afore_bnt_t6_96k: limitsFromRated(96000),
  afore_bnt_t6_110k: limitsFromRated(110000),
  // TSUN TSOL-MS microinverters (Wave A brand-expansion batch 4). The full
  // TSOL-MS line is 300-2000 W = 6.67x, so it cannot be one entry; the two
  // bands split on MPPT count (1 vs 2), which satisfies the 2x rule as a side
  // effect at 1.33x each. **No battery of any kind** — a microinverter has no
  // storage, so neither id is in HV_BATTERY_MODELS and neither ever could be.
  //
  // These are by far the smallest entries in the catalogue (400 W is 3.75x
  // smaller than the previous minimum, srne_hsi_1500 at 1500 W), which makes
  // the 80 kW DEFAULT_LIMITS fallback off by 200x for them: without these rows
  // a garbled multi-kW reading would sail through as real production on a unit
  // that physically cannot make it.
  tsun_tsol_ms400: limitsFromRated(400),
  tsun_tsol_ms800: limitsFromRated(800),
  // Swatten SiH-TH three-phase hybrid (Wave A brand-expansion batch 4). ONE
  // entry: the SiH-TH family is 5/6/8/10 kW = **exactly 2.00x**, which does not
  // EXCEED the 2x threshold, so top-of-range is the correct tie-break. HV
  // battery (150-600 V) — it IS in HV_BATTERY_MODELS below, and must be:
  // without membership its pack voltage exceeds the 70 V LV ceiling on every
  // frame and reads 0 V forever.
  //
  // ⚠️ Do NOT widen this entry to Swatten's other families. SiH-…-SL is a
  // single-phase LOW-voltage line (42-59 V pack); folding it in here would
  // apply a 1000 V battery ceiling to a 48 V-class pack.
  swatten_sih_th_10k: limitsFromRated(10000),
  // CHINT / CPS SCE single-phase LV hybrid (Wave A brand-expansion batch 4).
  // ONE entry, covering Family A only (`SCE…KTL-60/120/EU`, 3.6-6.0 kW =
  // 1.67x, inside the 2x rule). 40-60 V LV pack — NOT in HV_BATTERY_MODELS.
  //
  // ⚠️ Do NOT stretch this to CHINT's commercial SCA/SCH string range
  // (25-350 kW = 14x): a 350 kW ceiling on a 6 kW hybrid gives maxPvPowerW
  // 700 kW and disables plausibility clamping entirely for the customers who
  // actually own this model. That range is not covered by the register map at
  // all, and SunSpec is the right path for it. The three-phase HV Family B
  // (`SCE…K-TH-EU`, 150-600 V battery) is likewise absent, and folding it in
  // here would apply this entry's 70 V battery ceiling to an HV pack.
  chint_cps_sce_6k: limitsFromRated(6000),
  // INVT XD3-6KTL single-phase hybrid (Wave A brand-expansion batch 4). ONE
  // entry, scoped to the sub-line the profile was tested against: INVT's own
  // ESS Catalog gives 3 / 3.68 / 4 / 4.6 / 5 / 6 kW for XD3KTL…XD6KTL =
  // exactly 2.00x, inside the 2x rule. 40-60 V LV pack — NOT HV.
  //
  // ⚠️ The upstream profile header claims `3.6~136kW` (37.8x) and is wrong on
  // BOTH ends; it conflates the XD hybrid line with the batteryless XG
  // grid-tie line (1-150 kW), which has no catalogue entry. If this entry is
  // ever widened to `XD…KTL` generally the operative figure becomes 3.33x
  // (XD7/XD8/XD10KTL) and it WOULD need a split — do not widen it silently.
  invt_xd_6k: limitsFromRated(6000),
  // LuxPower / EG4 (Wave C brand-expansion — Modbus TCP through an RS-485
  // gateway, not Solarman). FOUR entries: the family runs 3 kW → 21 kW = **7x**,
  // so one entry is impossible under the 2x rule and the split is per
  // nameplate rather than per band — each of these IS a single nameplate.
  //
  // ⚠️ ratedPowerW is read off the MODEL NAMES, which for this family encode kW
  // unambiguously (`18kPV` = 18 kW, `FlexBOSS21` = 21 kW, `LXP-LB-EU 7K` =
  // 7 kW). The protocol specification carries no nameplate ratings and no
  // datasheet was reachable. Confirm against a datasheet before treating any
  // ceiling as authoritative.
  //
  // ALL FOUR are 48 V-class LV packs and NONE is in HV_BATTERY_MODELS — that is
  // measured, not assumed: the two live register dumps this map rests on read
  // Vbat 53.6 V (18kPV) and 52.9 V (FlexBOSS21). Adding one here would raise
  // its battery ceiling from 70 V to 1000 V and the corrupt-frame guard would
  // stop catching anything.
  eg4_18kpv: limitsFromRated(18000),
  eg4_12kpv: limitsFromRated(12000),
  eg4_flexboss21: limitsFromRated(21000),
  luxpower_lxp_lb_eu_7k: limitsFromRated(7000),
  // SolaX X3-Hybrid G4 — Modbus TCP through an RS-485 gateway, same transport
  // class as the LuxPower/EG4 entries above.
  //
  // ONE band, 15 kW, because that is the only nameplate with evidence (a live
  // capture plus the vendor datasheet column for that exact model). The family
  // runs 5-15 kW = 3x, past the 2x rule, so the other members need their own
  // entries — a shared one would give a 5 kW owner a 30 kW PV ceiling.
  //
  // Every derived bound was checked against the datasheet rather than assumed:
  // maxPvPowerW 30000 is exactly its "Max. recommended PV array power 30 kWp",
  // and maxGridPowerW 22500 sits above its "Max. AC input apparent power
  // 20 kVA", so no maxGridPowerW override is needed. HV pack — see below.
  solax_x3_hybrid_g4_15k: limitsFromRated(15000),
  // Edecoa EM-624A: a Voltronic PI30 clone wired to the edge board's RS-485
  // port (added 2026-09-23). 6.2 kW, 48 V pack, single phase. LV, so NOT in
  // HV_BATTERY_MODELS, and absent from LV_PACK_NOMINAL_V because it is 48 V.
  //
  // ⚠️ FROM THE PRODUCT PAGE AND THE MANUAL, NOT THE NAMEPLATE. The nameplate
  // photo is still owed; re-check every figure here when it arrives
  // (docs/inverter-research/2026-09-23-edecoa-em-624a.md, "Phase 2 -
  // nameplate envelope").
  //
  // maxGridPowerW is overridden for the Anenji reason: in line mode the AC
  // charger runs at the same time as the load, so peak grid draw is load plus
  // charging. 6200 W load + 80 A (the manual's AC charging maximum) x 58.4 V
  // bulk = 10 872 W before charger losses, over the derived 1.5 x 6200 =
  // 9300 W. 11 500 W leaves room for those losses. Over the ceiling the
  // validator ZEROES `gridPower` and keeps the reading, so a ceiling that is
  // too low shows the owner a confident 0 W import whenever the charger runs
  // at full current.
  // PI30 carries no grid power, so the edge decoder omits `gridPower` and this
  // ceiling guards only a value some later path derives.
  // Mirrored by `maxGridPowerWOverride` in models_catalog.dart.
  //
  // Two guards below were written for a flat lithium curve, and this unit's
  // owner has a plain battery whose SOC the inverter ESTIMATES from voltage.
  // Neither is changed for this model yet:
  //   - the LV corrupt-frame floor (40 V for a 48 V pack) sits above the
  //     manual's lowest low-DC cut-off (38.4 V, AGM or gel at high load);
  //   - the proportional corrupt-frame detector can read a heavy-load sag, in
  //     which SOC and voltage fall together, as a corrupt frame.
  // Both drop the whole reading. Decide from the cut-off the customer has
  // set (QPIRI field 9) and from a live sag, not from the manual.
  edecoa_em_624a: {
    ...limitsFromRated(6200),
    maxGridPowerW: 11500,
  },
  // LuxPower SNA5000 WPV: single-phase off-grid hybrid on the edge board's
  // RS-485 port at 19200 baud (added 2026-09-24). 5 kW, 48 V pack. LV, so NOT
  // in HV_BATTERY_MODELS, and absent from LV_PACK_NOMINAL_V because it is 48 V.
  //
  // Figures from the owner's nameplate photo (2026-09-24): AC input 40 A,
  // AC charge 60 A, solar charge 110 A, battery 38.4-60 V. Nothing measured
  // (docs/inverter-research/2026-09-24-luxpower-sna-5000-wpv.md, Phase 2).
  //
  // maxGridPowerW is overridden for the Anenji reason: in line mode the AC
  // charger runs beside the load. 5000 W load + 60 A x 58.4 V bulk = 8504 W
  // before losses, over the derived 7500 W. 9200 W is the AC input's own
  // rating (40 A x 230 V). Mirrored by `maxGridPowerWOverride` in
  // models_catalog.dart.
  //
  // maxBatteryPowerW is overridden because solar charging alone is
  // 110 A x 58.4 V = 6424 W, over the derived 6000 W. Over either ceiling the
  // validator ZEROES that field, so a low one shows a confident 0 W exactly
  // when the charger runs hardest. The catalogue has no battery-ceiling field
  // to mirror this; the phone never harvests this model.
  //
  // The two Edecoa open questions apply unchanged: the LV corrupt-frame floor
  // (40 V) sits above the nameplate's 38.4 V, and the proportional detector
  // can read a lead-acid sag as a corrupt frame. Decide from the owner's
  // configured cut-off and a live sag.
  luxpower_sna_5000_wpv: {
    ...limitsFromRated(5000),
    maxGridPowerW: 9200,
    maxBatteryPowerW: 6700,
  },
  // MUST PV1800 family (PV18, PH18, PV19), added 2026-10-24: off-grid
  // hybrids on Modbus RTU at slave 4. Nothing measured: ratings and charger
  // figures come from MUST datasheets and retail listings
  // (docs/inverter-research/2026-10-24-must-pv1800.md, Phase 2 table, which
  // names each figure's source).
  //
  // maxGridPowerW is overridden on every entry for the Anenji reason: in line
  // mode the AC charger runs beside the load, so peak grid draw is
  // rated + AC charge A x bulk V (14.6 / 29.2 / 58.4 V) x 1.1, and never below
  // the derived 1.5x rated. Over the ceiling the validator zeroes gridPower.
  // Mirrored by `maxGridPowerWOverride` in models_catalog.dart.
  //
  // The 12 V and 24 V ids are also in LV_PACK_NOMINAL_V below; absent there, a
  // healthy 24 V pack reads as a corrupt 48 V frame and every reading drops.
  must_pv18_1012: {
    ...limitsFromRated(1000),
    maxGridPowerW: 1500,
  },
  must_pv18_1512: {
    ...limitsFromRated(1500),
    maxGridPowerW: 2250,
  },
  must_pv18_2012: {
    ...limitsFromRated(2000),
    maxGridPowerW: 3285,
  },
  must_pv18_3024: {
    ...limitsFromRated(3000),
    maxGridPowerW: 4928,
  },
  must_pv18_3224: {
    ...limitsFromRated(3200),
    maxGridPowerW: 5128,
  },
  must_pv18_3524: {
    ...limitsFromRated(3500),
    maxGridPowerW: 5428,
  },
  must_pv18_3624: {
    ...limitsFromRated(3600),
    maxGridPowerW: 5528,
  },
  must_pv18_4024: {
    ...limitsFromRated(4000),
    maxGridPowerW: 6570,
  },
  must_pv18_5048: {
    ...limitsFromRated(5000),
    maxGridPowerW: 8855,
  },
  must_pv18_5248: {
    ...limitsFromRated(5200),
    maxGridPowerW: 9055,
  },
  must_pv18_6048: {
    ...limitsFromRated(6000),
    maxGridPowerW: 12424,
  },
  must_ph18_5248: {
    ...limitsFromRated(5200),
    maxGridPowerW: 10340,
  },
  must_pv19_4024: {
    ...limitsFromRated(4000),
    maxGridPowerW: 6570,
  },
  must_pv19_6048: {
    ...limitsFromRated(6200),
    maxGridPowerW: 12624,
  },
};

// Fallback for unknown models — generous limits that still catch garbled data
const DEFAULT_LIMITS = limitsFromRated(80000);

/**
 * Per-string PV power below this (watts) on a secondary MPPT input (PV3/PV4)
 * is treated as floating-input ADC noise, not real production, and clamped to
 * zero at ingestion. Chosen above the largest noise sample observed on a
 * 2-MPPT install (46.9 W) and well below any meaningful string output.
 */
const PV_STRING_NOISE_FLOOR_W = 50;

/**
 * Gen-port AC voltage (volts) below MIN or above MAX means the port is not
 * energized by a running generator. On hybrids with no generator connected the
 * gen-port power register floats to phantom spikes; the inverter only reports a
 * real gen voltage (≈230V single-phase, ≈400V line-to-line) when the port is
 * actually energized. Mirrors the mobile-harvester gate in
 * packages/inverter_protocol/lib/src/readers/solarman_reader.dart.
 */
const GENERATOR_ENERGIZED_MIN_V = 80;
const GENERATOR_ENERGIZED_MAX_V = 300;

/** Models with high-voltage battery packs (200-1000V) */
/**
 * Nominal pack voltage for LV models that are NOT 48 V-class. Anything absent
 * is 48 V, which is the overwhelming majority.
 *
 * Only the corrupt-frame floor reads this. Adding a model here WIDENS what is
 * accepted for it, so an entry needs a nameplate to back it -- a wrong value
 * makes the guard stop guarding.
 */
const LV_PACK_NOMINAL_V: Record<string, number> = {
  // Nameplate read 2026-08-21: DC input 24 VDC, AC charger DC output 27 VDC.
  anenji_anj_4200: 24,
  // The catalogue comment beside its MODEL_LIMITS entry already said 12 V; the
  // guard just never knew.
  srne_hsi_1500: 12,
  // MUST PV1800 family: pack voltage from the model name (the last two digits)
  // and the datasheets. Held to the catalogue's nominalPackVoltage by
  // pack_voltage_parity_test.dart in packages/inverter_protocol.
  must_pv18_1012: 12,
  must_pv18_1512: 12,
  must_pv18_2012: 12,
  must_pv18_3024: 24,
  must_pv18_3224: 24,
  must_pv18_3524: 24,
  must_pv18_3624: 24,
  must_pv18_4024: 24,
  must_pv19_4024: 24,
};

const HV_BATTERY_MODELS = new Set([
  'deye_sg01hp3', 'deye_sg01hp3_50k', 'deye_sg01hp3_30k', 'deye_sg02hp3_80k', 'deye_gb_s20k',
  // ⚠️ MECHANISM, corrected in fix round 2 — the first version of this comment
  // named the wrong guard. What membership actually controls is
  // `maxBatteryVoltage` (line ~660): `HV_BATTERY_MODELS.has(modelId) ? 1000 : 70`.
  // Omit an HV model and its 125-600 V / 300-400 V pack voltage exceeds 70 V on
  // EVERY frame, so `sanitized.batteryVoltage` is set to 0 with a warning, every
  // reading, forever — the user sees a 0 V pack. (In the corner where SOC also
  // reads ≤1 % and load reads 0, the zero-block guard then escalates it to
  // `communicationFailure`.)
  //
  // It is NOT the LV "battery voltage below 40 V is a corrupt frame" guard
  // (line ~1058): that fires only for `0 < batteryVoltage < 40`, which a
  // 320-400 V pack never enters. Membership was right; the reasoning printed
  // beside it was not.
  //
  // Megarevo R…KH3: the "H" in the nameplate is the HV-battery marker and the
  // R12KH3's own spec sheet reads 125-600 V.
  'megarevo_r_h3_10k', 'megarevo_r_h3_15k',
  // Afore AF…K-TH: an HV pack — the vendor's `InverCharacter` bits B0-B2 encode
  // `2 = HV hybrid` for this line, and the one live capture read 321.2 V. ⚠️ No
  // Afore source states a pack VOLTAGE RANGE; the "170-500 V" that appeared here
  // in the first cut was transplanted from the Eaton xStorage Home 3P's
  // Pylontech pack in the KSTAR research and is withdrawn. The twelve BNT
  // entries are grid-tie strings with no battery and are deliberately absent.
  // The hybrid entries are scoped to the THREE-PHASE TH family for exactly this
  // reason — the single-phase AF…K-SL is a 48 V LV product, and putting both on
  // one entry would apply a 1000 V ceiling to a 48 V pack.
  'afore_hybrid_6k', 'afore_hybrid_12k', 'afore_hybrid_24k', 'afore_hybrid_30k',
  // Swatten SiH-TH: the datasheet's own battery range is 150-600 V, and the
  // profile header says "HV Battery Supported". Mirrored by
  // `SwattenSihThRegisters.isHighVoltage` on the Dart side — the two must stay
  // in step, and both directions are asserted in the test suites. Scoped to
  // the THREE-PHASE TH family deliberately: the single-phase SiH-…-SL is a
  // 42-59 V LV product and has no catalogue entry.
  'swatten_sih_th_10k',
  // SolaX X3-Hybrid G4: the datasheet's own battery row reads **120 ~ 800 V**,
  // so this is vendor-stated rather than inferred from one reading — and the
  // one live capture (240.2 V) sits inside it. Mirrored by
  // `SolaxX3HybridG4Registers.isHighVoltage` on the Dart side; the two must
  // stay in step and both directions are asserted in the suites.
  //
  // Scoped to the THREE-PHASE X3-Hybrid G4 deliberately. SolaX's single-phase
  // X1 lines and its LV products must never be folded onto this id, or a 48 V
  // pack would be checked against a 1000 V ceiling and the guard stops
  // catching anything.
  'solax_x3_hybrid_g4_15k',
  // Sofar HYD 5-20KTL-3PH: the datasheet's battery range is 180-800 V. Both
  // ids were left out until 2026-10-13 on the belief that the pack is 48 V,
  // so a phone station showed 0 V for a 409.6 V pack. Mirrored by
  // `SofarHyd3PhRegisters.isHighVoltage` on the Dart side.
  'sofar_hyd_3ph_10k', 'sofar_hyd_3ph_20k',
  // Solis S6-EH3P(12-20)K-H: the datasheet's battery range is 120-800 V.
  // Mirrored by `SolisHybrid3PHvRegisters.isHighVoltage`.
  'solis_s6_eh3p_h',
]);

/**
 * True when the model uses a high-voltage battery pack (hundreds of volts)
 * rather than the LV 48V class. Drives the charge-voltage bounds the mobile
 * custom-event builder presents (see helpers.buildCalendarCapabilities).
 */
export function isHvBatteryModel(modelId: string | undefined): boolean {
  return !!modelId && HV_BATTERY_MODELS.has(modelId);
}

/**
 * Compute the maximum plausible daily energy jump (kWh) given the inverter's
 * rated power and the time gap between readings.
 *
 * Scales linearly: an 8kW inverter over 60 min → 8 kWh max jump.
 * Floor of 5 minutes prevents false positives on near-simultaneous readings.
 * Capped at maxDailyEnergyKwh to stay within physical daily limits.
 */
export function computeMaxEnergyJumpKwh(ratedPowerW: number, minutesDelta: number): number {
  const effectiveMinutes = Math.max(minutesDelta, 5); // floor: 5 min minimum
  const maxJump = (ratedPowerW / 1000) * (effectiveMinutes / 60);
  const maxDaily = (ratedPowerW / 1000) * 10; // same as maxDailyEnergyKwh
  return Math.min(maxJump, maxDaily);
}

export function getLimitsForModel(modelId: string): ModelLimits {
  return MODEL_LIMITS[modelId] || DEFAULT_LIMITS;
}

export function getNominalAcPowerW(modelId: string): number {
  return (MODEL_LIMITS[modelId] || DEFAULT_LIMITS).nominalAcPowerW;
}

/**
 * True when `modelId` is in the catalogue (so `getNominalAcPowerW` returns a
 * real rated AC power rather than the generous unknown-model default). Lets
 * callers (e.g. the forecast AC-clip) fall back to a DC:AC ratio for models
 * we don't have a rating for, instead of the 80 kW default that never clips.
 */
export function isKnownInverterModel(modelId: string | undefined): boolean {
  return !!modelId && Object.prototype.hasOwnProperty.call(MODEL_LIMITS, modelId);
}

/**
 * Resolve the nameplate AC power used as the bonus-energy baseline for one
 * inverter: `max(0, pvPower - nominalAcPowerW)` integrated over the hour.
 *
 * ISSUE #22. `inverters/{id}.nominalAcPowerW` is OPTIONAL and nothing forces it
 * to be written, so every rollup cfg-construction site used to pass `undefined`
 * straight through — and both `rollupReadings5MinToHourly` and
 * `foldHourAccumulator` gate the whole bonus computation on
 * `nominalAcPowerW != null`. Result: «Бонусна генерація» vanished from Reports
 * with no error and no diagnostic for every household whose doc lacks it.
 *
 * Unlike `genPortMode`, there is NO safe numeric default: 0 would make ALL
 * production count as bonus surplus, which is worse than the silence it
 * replaces. The correct fallback is the model's nameplate AC rating, which
 * MODEL_LIMITS already carries — `nominalAcPowerW: ratedPowerW` in
 * `limitsFromRated` means exactly this.
 *
 * For a model NOT in the catalogue we deliberately return `undefined` rather
 * than DEFAULT_LIMITS' 80 kW: an 80 kW baseline on a real 100 kW string
 * inverter silently guarantees zero bonus forever — the same invisible failure
 * in a new costume. `undefined` at least keeps the feature honestly absent
 * instead of wrong.
 *
 * @param docValue raw `inverters/{id}.nominalAcPowerW` (any type — Firestore).
 * @param modelId  the inverter's `model` / `modelId`.
 */
export function resolveNominalAcPowerW(
  docValue: unknown,
  modelId: string | undefined,
): number | undefined {
  if (typeof docValue === 'number' && Number.isFinite(docValue) && docValue > 0) {
    return docValue;
  }
  if (!isKnownInverterModel(modelId)) return undefined;
  return getNominalAcPowerW(modelId as string);
}

export interface ValidationResult {
  /** True if reading passes all checks and should be stored as-is */
  valid: boolean;
  /** Fields with anomalous values — reading is still stored but these are flagged */
  warnings: string[];
  /** Fields that were clamped/corrected — the corrected reading is in sanitizedData */
  corrected: string[];
  /** Set when an energy balance violation triggered rejection. Used for Sentry context. */
  energyBalance?: {
    side: 'load' | 'charge';
    residualW: number;
    thresholdW: number;
    reason: string;
  };
}

/**
 * Power the frame's sinks demand that its declared sources cannot supply, in
 * watts. Positive means "this much arrived from somewhere not in this reading".
 *
 * Deliberately the same terms `validateReading`'s balance checks use, so the
 * two cannot drift: sinks are load + battery charge + grid export, sources are
 * PV + battery discharge + grid import. `generatorPower` is excluded — it is
 * the unknown being solved for.
 */
function sinkDeficitW(r: ReadingLike): number {
  const batteryCharge = Math.max(0, r.batteryPower);
  const batteryDischarge = Math.max(0, -r.batteryPower);
  const gridImport = Math.max(0, r.gridPower);
  const gridExport = Math.max(0, -r.gridPower);
  const sinks = (r.loadPower ?? 0) + batteryCharge + gridExport;
  const sources = r.pvPower + batteryDischarge + gridImport;
  return sinks - sources;
}

/** Span of a 16-bit word: a signed decode of `x` reads `x - 65536` above 32767. */
const UINT16_SPAN = 65536;

/**
 * The true watts behind a negative `generatorPower`, or `undefined` when the
 * value cannot be trusted and must be stripped instead (#477's ruling).
 *
 * Both gates must hold. The rating gate uses the same 1.5x slack as
 * `maxLoadPowerW`/`maxGridPowerW`, and an UNKNOWN model has no rating to check
 * against — `DEFAULT_LIMITS`' 80 kW would wave through every wrap on a 6 kW
 * unit — so an unknown model never recovers.
 */
function recoverWrappedGeneratorPower(
  data: ReadingLike,
  sanitized: ReadingLike,
  limits: ModelLimits,
  modelId: string,
): number | undefined {
  const raw = data.generatorPower;
  if (raw === undefined || raw >= 0) return undefined;
  // Only an int16 wrap is recoverable; anything below -32768 is not one.
  if (!Number.isInteger(raw) || raw < -32768) return undefined;
  if (!isKnownInverterModel(modelId)) return undefined;

  const unwrapped = raw + UINT16_SPAN;
  if (unwrapped > Math.round(limits.ratedPowerW * 1.5)) return undefined;

  // Corroboration: the frame must independently be short by about this much.
  // The tolerance is proportional because sinks and sources are sampled across
  // several Modbus batches, so a real frame never balances to the watt.
  const deficit = sinkDeficitW(sanitized);
  const tolerance = Math.max(1000, 0.25 * unwrapped);
  if (Math.abs(unwrapped - deficit) > tolerance) return undefined;

  return unwrapped;
}

/**
 * Validate and sanitize a reading against model-specific and physical limits.
 *
 * Returns warnings for anomalous fields and a sanitized copy where out-of-range
 * values are clamped to zero. The API stores the sanitized version and returns
 * the warnings so the caller knows what was off.
 *
 * This is the server-side safety net — the mobile harvester already does thorough
 * validation in _validateAndMerge, but the ESP32 edge connector sends raw data.
 */
export function validateReading<T extends ReadingLike>(
  data: T,
  inverterData: {
    modelId?: string;
    hasBattery?: boolean;
    genPortMode?: number;
    /** Configured number of physical PV strings (MPPT inputs). Any per-string
     *  power beyond this count is a phantom read and is clamped to zero. */
    pvStrings?: number;
    /** Last stored battery SOC/voltage (from current/latest) — used to detect a
     *  corrupt frame where SOC and voltage collapse by the same proportion. */
    prevBatterySoc?: number;
    prevBatteryVoltage?: number;
    /** Battery chemistry + CONFIGURED series cell count — feed the SOC-vs-voltage
     *  corrupt-frame guard below. The count must be the configured value, never
     *  inferred from the (possibly corrupt) voltage. */
    batteryChemistry?: string | null;
    batteryCellCount?: number | null;
    /** Flag-gate for the SOC-vs-voltage corrupt-frame guard. OFF unless the
     *  caller explicitly enables it (staged rollout / instant kill-switch). */
    batteryCorruptGuardEnabled?: boolean;
  },
): ValidationResult & { sanitizedData: T } {
  const warnings: string[] = [];
  const corrected: string[] = [];
  let energyBalance: ValidationResult['energyBalance'];
  /** Set when a SOURCE field was stripped as non-physical, i.e. its true
   *  magnitude is unknown rather than zero. Read by the energy-balance
   *  checks below, which cannot call a deficit impossible while the source
   *  total has a hole in it (#477). */
  let sourcesIncomplete = false;
  const modelId = inverterData.modelId || '';
  const limits = MODEL_LIMITS[modelId] || DEFAULT_LIMITS;

  // Start with a copy of the data
  const sanitized = { ...data };

  // ═══════════════════════════════════════════════════════════════════════════
  // CATEGORY 1: Sign and Polarity Violations (physically impossible negatives)
  // ═══════════════════════════════════════════════════════════════════════════

  // PV power must be >= 0 (solar panels cannot consume power)
  if (data.pvPower < 0) {
    warnings.push(`pvPower ${data.pvPower}W is negative — solar panels cannot consume power`);
    sanitized.pvPower = 0;
    corrected.push('pvPower');
  }

  // Load power must be >= 0 (loads consume, not generate).
  // Unlike the over-limit branch further down this ZEROES rather than strips: a
  // negative load is not an untrustworthy magnitude, it is a sign error on a
  // quantity that cannot be negative, and zero is its correct floor.
  if (data.loadPower !== undefined && data.loadPower < 0) {
    warnings.push(`loadPower ${data.loadPower}W is negative — loads cannot generate power`);
    sanitized.loadPower = 0;
    corrected.push('loadPower');
  }

  // Battery SOC must be 0-100% (when present — the edge firmware omits it
  // during cold-start warmup, meaning "unknown", which must not be clamped).
  if (data.batterySoc != null) {
    if (data.batterySoc < 0) {
      warnings.push(`batterySoc ${data.batterySoc}% is negative`);
      sanitized.batterySoc = 0;
      corrected.push('batterySoc');
    } else if (data.batterySoc > 100) {
      warnings.push(`batterySoc ${data.batterySoc}% exceeds 100%`);
      sanitized.batterySoc = 100;
      corrected.push('batterySoc');
    }
  }

  // Battery voltage must be >= 0
  if (data.batteryVoltage < 0) {
    warnings.push(`batteryVoltage ${data.batteryVoltage}V is negative`);
    sanitized.batteryVoltage = 0;
    corrected.push('batteryVoltage');
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // CATEGORY 2: Register Error Codes (common Modbus fault indicators)
  // ═══════════════════════════════════════════════════════════════════════════

  // 0xFFFF (65535) and 0x7FFF (32767) often indicate read errors
  const REGISTER_ERROR_VALUES = [65535, 32767, 32768, -32768];
  const checkRegisterError = (field: string, value: number | undefined): boolean => {
    if (value === undefined) return false;
    if (REGISTER_ERROR_VALUES.includes(value) || REGISTER_ERROR_VALUES.includes(Math.abs(value))) {
      warnings.push(`${field}=${value} appears to be a register error code (0xFFFF/0x7FFF)`);
      return true;
    }
    return false;
  };

  if (checkRegisterError('pvPower', data.pvPower)) {
    sanitized.pvPower = 0;
    corrected.push('pvPower');
  }
  if (checkRegisterError('gridPower', data.gridPower)) {
    sanitized.gridPower = 0;
    corrected.push('gridPower');
  }
  if (checkRegisterError('loadPower', data.loadPower)) {
    sanitized.loadPower = 0;
    corrected.push('loadPower');
  }
  if (checkRegisterError('batteryPower', data.batteryPower)) {
    sanitized.batteryPower = 0;
    corrected.push('batteryPower');
  }

  // Check for multiple simultaneous register errors — likely communication failure
  const POWER_FIELDS = ['pvPower', 'gridPower', 'loadPower', 'batteryPower'];
  const registerErrorFields = POWER_FIELDS.filter(f => corrected.includes(f));
  if (registerErrorFields.length >= 3) {
    warnings.push(
      `Multiple power fields (${registerErrorFields.join(', ')}) show register errors — ` +
      `likely communication failure, entire reading is unreliable`
    );
    // Mark as communication failure for caller to potentially skip storage
    corrected.push('communicationFailure');
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // CATEGORY 3: Model-Specific Power Limits
  // ═══════════════════════════════════════════════════════════════════════════

  // PV power
  if (data.pvPower > limits.maxPvPowerW) {
    warnings.push(`pvPower ${data.pvPower}W exceeds max ${limits.maxPvPowerW}W for ${modelId || 'unknown model'}`);
    sanitized.pvPower = 0;
    corrected.push('pvPower');
  }
  if (data.pvPower1 !== undefined && data.pvPower1 > limits.maxPvPowerW) {
    warnings.push(`pvPower1 ${data.pvPower1}W exceeds max ${limits.maxPvPowerW}W`);
    sanitized.pvPower1 = 0;
    corrected.push('pvPower1');
  }
  if (data.pvPower2 !== undefined && data.pvPower2 > limits.maxPvPowerW) {
    warnings.push(`pvPower2 ${data.pvPower2}W exceeds max ${limits.maxPvPowerW}W`);
    sanitized.pvPower2 = 0;
    corrected.push('pvPower2');
  }
  // PV3/PV4 (4-MPPT inverters, e.g. SG01HP3 50K)
  if (data.pvPower3 !== undefined && data.pvPower3 > limits.maxPvPowerW) {
    warnings.push(`pvPower3 ${data.pvPower3}W exceeds max ${limits.maxPvPowerW}W`);
    sanitized.pvPower3 = 0;
    corrected.push('pvPower3');
  }
  if (data.pvPower4 !== undefined && data.pvPower4 > limits.maxPvPowerW) {
    warnings.push(`pvPower4 ${data.pvPower4}W exceeds max ${limits.maxPvPowerW}W`);
    sanitized.pvPower4 = 0;
    corrected.push('pvPower4');
  }
  // PV5/PV6 (6-MPPT inverters, e.g. SG02HP3 80K)
  if (data.pvPower5 !== undefined && data.pvPower5 > limits.maxPvPowerW) {
    warnings.push(`pvPower5 ${data.pvPower5}W exceeds max ${limits.maxPvPowerW}W`);
    sanitized.pvPower5 = 0;
    corrected.push('pvPower5');
  }
  if (data.pvPower6 !== undefined && data.pvPower6 > limits.maxPvPowerW) {
    warnings.push(`pvPower6 ${data.pvPower6}W exceeds max ${limits.maxPvPowerW}W`);
    sanitized.pvPower6 = 0;
    corrected.push('pvPower6');
  }

  // Per-string noise floor for the secondary MPPT inputs (PV3–PV6).
  //
  // The Deye SG04LP3/SG05LP3 register map polls the PV3/PV4 power registers
  // (674/675) on every read. On a 2-MPPT install those inputs are unconnected
  // and float to a few watts of ADC noise (observed 3.5 W / 46.9 W on RubHome's
  // master, 2026-06-13). A single noisy sample is enough to surface a permanent
  // phantom "PV3/PV4" series in the app chart legend. Clamp sub-floor values to
  // zero so they never reach Firestore. The floor is intentionally applied only
  // to PV3–PV6 — the primary strings (PV1/PV2) keep their genuine low-light
  // readings, and the separate total `pvPower` register is untouched either way.
  const isBelowStringNoiseFloor = (v: number | undefined): boolean =>
    v !== undefined && v > 0 && v < PV_STRING_NOISE_FLOOR_W;
  if (isBelowStringNoiseFloor(sanitized.pvPower3)) {
    sanitized.pvPower3 = 0;
    corrected.push('pvPower3');
  }
  if (isBelowStringNoiseFloor(sanitized.pvPower4)) {
    sanitized.pvPower4 = 0;
    corrected.push('pvPower4');
  }
  if (isBelowStringNoiseFloor(sanitized.pvPower5)) {
    sanitized.pvPower5 = 0;
    corrected.push('pvPower5');
  }
  if (isBelowStringNoiseFloor(sanitized.pvPower6)) {
    sanitized.pvPower6 = 0;
    corrected.push('pvPower6');
  }

  // Config-aware phantom-string clamp.
  //
  // The configured `pvStrings` count is authoritative — it's set during
  // onboarding to the number of physically connected MPPT inputs. The register
  // maps poll more string-power registers than that (e.g. the edge 1-phase
  // layout reads reg 188 as "PV3 power" for every 1-phase Deye, and the
  // SG04LP3 map polls PV3/PV4). On an install with fewer strings those extra
  // registers float to phantom values — sometimes *large* ones (666 W observed
  // on a 2-MPPT deye_sg05lp1 during the morning MPPT ramp, 2026-06-22), well
  // above the per-string noise floor and below the model max. Clamp any string
  // beyond the configured count to zero regardless of magnitude. This runs
  // BEFORE the pvPower≈Σstrings cross-check below so a phantom string can't
  // inflate the total. Only applied when pvStrings is known (>0); the noise
  // floor above still covers inverters without a configured count.
  if (typeof inverterData.pvStrings === 'number' && inverterData.pvStrings > 0) {
    const phantomStrings: Array<['pvPower3' | 'pvPower4' | 'pvPower5' | 'pvPower6', number]> = [
      ['pvPower3', 3],
      ['pvPower4', 4],
      ['pvPower5', 5],
      ['pvPower6', 6],
    ];
    const stringSumBeforeClamp = [
      sanitized.pvPower1,
      sanitized.pvPower2,
      sanitized.pvPower3,
      sanitized.pvPower4,
      sanitized.pvPower5,
      sanitized.pvPower6,
    ].reduce<number>((a, b) => a + (typeof b === 'number' && b > 0 ? b : 0), 0);
    let phantomWatts = 0;
    for (const [field, stringIndex] of phantomStrings) {
      if (stringIndex > inverterData.pvStrings && sanitized[field] !== undefined && sanitized[field]! > 0) {
        warnings.push(
          `${field} ${sanitized[field]}W on a ${inverterData.pvStrings}-string inverter — phantom MPPT read, clamped to 0`,
        );
        phantomWatts += sanitized[field]!;
        sanitized[field] = 0;
        corrected.push(field);
      }
    }

    // Zeroing the phantom string is not enough when the total was BUILT from the
    // strings. The edge firmware sums them —
    // `reading->pv_power += reading->pv_power_3` in
    // devices/edge-connector-v2/components/solarman/register_apply.c — and its
    // shared 1-phase layout maps reg 188 to "PV3" for all fifteen 1-phase Deye
    // models, 97 of which are edge-connected in prod with <= 2 configured
    // strings. So the phantom watts arrive already inside pvPower, and clamping
    // only the per-string field leaves a PV total reading high (666 W observed)
    // beside a PV3 node correctly showing nothing. The cross-check below cannot
    // repair it: it is one-directional and fires only when the string sum
    // EXCEEDS the total (#361).
    //
    // Subtract only when the total demonstrably WAS the phantom-inclusive sum.
    //
    // A model whose pvPower is its own DC-input register is exempt outright,
    // because the tolerance test cannot tell "built from the strings" from
    // "the strings are complete". Once a Huawei SUN2000 reports one slot per
    // MPPT (#622), its slots sum to within 1% of the DC input on a healthy
    // unit, so the test below matches -- and with the default `pvStrings: 2`
    // every Huawei was onboarded with, it would cut a true 31.5 kW total to
    // 16.5 kW. Those slots are real MPPTs, not floating registers; the clamp
    // above still hides the ones beyond the configured count.
    if (phantomWatts > 0 && typeof sanitized.pvPower === 'number' && !limits.pvTotalIsDcInputRegister) {
      const tolerance = Math.max(50, stringSumBeforeClamp * 0.05);
      if (Math.abs(sanitized.pvPower - stringSumBeforeClamp) <= tolerance) {
        const withoutPhantom = Math.max(0, sanitized.pvPower - phantomWatts);
        warnings.push(
          `pvPower ${sanitized.pvPower}W was the sum of the string registers including ` +
          `${phantomWatts}W of phantom MPPT reads — reduced to ${withoutPhantom}W`,
        );
        sanitized.pvPower = withoutPhantom;
        corrected.push('pvPower');
      }
    }
  }

  // Cross-check: pvPower should approximately equal the sum of all active PV
  // strings. Supports inverters with up to 6 MPPT inputs (e.g. SG02HP3 80K).
  // If the strings report much higher than the total, the total register is
  // stuck/corrupted — fall back to the string sum. Requires ≥2 active strings
  // so a single-string inverter isn't cross-validated against itself.
  {
    const strings = [
      sanitized.pvPower1,
      sanitized.pvPower2,
      sanitized.pvPower3,
      sanitized.pvPower4,
      sanitized.pvPower5,
      sanitized.pvPower6,
    ].filter((v): v is number => v !== undefined && v > 0);
    if (strings.length >= 2) {
      const stringSum = strings.reduce((a, b) => a + b, 0);
      // ONE-DIRECTIONAL by design (2026-07-30): only correct when the SUM
      // EXCEEDS the total — that is the stuck/corrupted-total-register case
      // this guard exists for. The reported strings are not guaranteed to be
      // exhaustive: Huawei SUN2000 units report the true DC-aggregate total
      // but per-string detail for only strings 1+2, so a many-string 150K ran
      // 84 kW while its two reported strings summed to 8.7 kW — the old
      // |diff| check "corrected" the true total down to 8.7 kW and the app
      // showed a 150 kW unit out-produced by a 30 kW one (Lemberg, refuted
      // by FusionSolar side-by-side). A total above the sum is information
      // the strings don't have; a sum above the total is corruption.
      const deficit = stringSum - sanitized.pvPower;
      // Allow 15% tolerance for timing/rounding, but flag if off by more than 500W AND 15%
      if (deficit > 500 && deficit > stringSum * 0.15) {
        warnings.push(
          `pvPower ${sanitized.pvPower}W below string sum ` +
          `(${strings.join(' + ')} = ${stringSum}W) — using string sum`
        );
        sanitized.pvPower = stringSum;
        corrected.push('pvPower');
      }
    }
  }

  // Grid power (signed)
  if (Math.abs(data.gridPower) > limits.maxGridPowerW) {
    warnings.push(`gridPower ${data.gridPower}W exceeds max ±${limits.maxGridPowerW}W`);
    sanitized.gridPower = 0;
    corrected.push('gridPower');
  }

  // Load power.
  //
  // STRIP, never zero (#53's ruling, applied here 2026-08-25). Zero is a
  // legitimate load, so writing it replaces a value we distrust with a
  // CONFIDENT WRONG one: the dashboard renders a flat "0 W" for a house that is
  // drawing power, and every consumer that averages or sums counts the zero as
  // a real sample. Absent says "not trusted", which is what we actually know.
  //
  // The cap is a heuristic and it now guards a DERIVED field.
  // `solax_x3_hybrid_g4_15k` publishes no house-load register on any revision,
  // so its loadPower is `pv - battery + grid` — the only field on the platform
  // that inherits two other fields' errors. A wrong grid sign does not merely
  // make grid wrong, it doubles into load and can cross this cap, and a zero
  // there is indistinguishable from a quiet house.
  if (data.loadPower !== undefined && data.loadPower > limits.maxLoadPowerW) {
    warnings.push(`loadPower ${data.loadPower}W exceeds max ${limits.maxLoadPowerW}W — field stripped`);
    delete sanitized.loadPower;
    corrected.push('loadPower');
  }

  // Battery power (signed)
  if (Math.abs(data.batteryPower) > limits.maxBatteryPowerW) {
    warnings.push(`batteryPower ${data.batteryPower}W exceeds max ±${limits.maxBatteryPowerW}W`);
    sanitized.batteryPower = 0;
    corrected.push('batteryPower');
  }

  // Generator power must be >= 0 (a gen port is a source, never a sink — smart
  // load mode reports its CONSUMPTION as a positive number too, and
  // `decomposeReading` already clamps this field with `Math.max(0, ...)`).
  //
  // STRIP, never zero (#53's ruling). Unlike a negative pvPower/loadPower this
  // is not a sign error on a quantity whose floor is 0: the observed shape is a
  // signed-decode WRAP of a large positive (-20721 W for a real 44,815 W, #312),
  // so the magnitude is untrustworthy, not the sign. Writing 0 would replace a
  // value we distrust with a confident wrong one AND keep the deficit that
  // rejects the frame; absent says "not trusted", which is what we know.
  //
  // RECOVER the wrap when the frame proves what it was (#312 cause 2). Deye 667
  // / Sunsynk 166 is a signed 16-bit word, so a generator above 32,767 W reads
  // as `raw - 65536`. Stripping is right when the magnitude is unknowable; it is
  // wrong when two independent facts pin it down:
  //
  //   1. the unsigned reinterpretation fits the model's rating, and
  //   2. the REST of the frame has a sink deficit of very nearly that size —
  //      load + battery charge + export, minus PV + discharge + import. On
  //      2026-09-02 the master read load 19 kW and charge 26 kW against 0.9 kW
  //      of PV: 44.1 kW of demand with nothing feeding it, and the wrap implies
  //      44,815 W. The number the frame asks for is the number the wrap holds.
  //
  // Requiring (2) is what keeps this from manufacturing nonsense. A phantom
  // -3 W on an idle gen port unwraps to 65,533 W, which a 50 kW inverter's
  // rating allows on paper — but a balanced frame has no deficit for it to
  // explain, so it is still stripped.
  //
  // THIS IS A CATEGORY-1 SIGN CHECK THAT DELIBERATELY RUNS LAST. It must sit
  // below the CATEGORY 2 register-error checks AND below the CATEGORY 3 grid /
  // load / battery clamps, because `sinkDeficitW` reads exactly those four
  // fields: a 0xFFFF batteryPower is 65,535 W of apparent charging, which is
  // the deficit a -1 W gen port unwraps to, so an unclamped frame lets the same
  // corruption that produced the wrap vouch for it. A field the clamps STRIP
  // (loadPower) then contributes 0 to the deficit, which biases toward refusing
  // recovery — the conservative direction. Do not move it back up.
  if (data.generatorPower !== undefined && data.generatorPower < 0) {
    const recovered = recoverWrappedGeneratorPower(data, sanitized, limits, modelId);

    if (recovered !== undefined) {
      warnings.push(
        `generatorPower ${data.generatorPower}W is a signed-decode wrap of ` +
        `${recovered}W — recovered; the frame's ${Math.round(sinkDeficitW(sanitized))}W ` +
        'unsourced demand corroborates it'
      );
      sanitized.generatorPower = recovered;
      corrected.push('generatorPower');
    } else {
      warnings.push(
        `generatorPower ${data.generatorPower}W is negative — a gen port cannot ` +
        'consume power; magnitude untrustworthy (signed-decode wrap?), field stripped'
      );
      delete sanitized.generatorPower;
      corrected.push('generatorPower');
      sourcesIncomplete = true;
    }
  }

  // Generator-port energization gate.
  //
  // The gen-port power register (reg 166 1-phase / 667 3-phase) floats to
  // isolated phantom spikes on hybrids with no generator connected. When the
  // reading carries a gen-port AC voltage, use it as the truth signal: a real
  // generator energizes the port to ≈230/400V, so power reported while the
  // voltage is out of the energized band (e.g. a floating 7.7V) is noise.
  // Only gate when generatorVoltage is present — older firmware and the mobile
  // harvester that don't send it are covered by the display-side
  // generatorIsReal gate instead. Mirrors solarman_reader.dart.
  if (
    sanitized.generatorPower !== undefined &&
    sanitized.generatorPower !== 0 &&
    sanitized.generatorVoltage !== undefined &&
    (sanitized.generatorVoltage < GENERATOR_ENERGIZED_MIN_V ||
      sanitized.generatorVoltage > GENERATOR_ENERGIZED_MAX_V)
  ) {
    warnings.push(
      `generatorPower ${sanitized.generatorPower}W ignored — gen port not ` +
      `energized (voltage ${sanitized.generatorVoltage}V outside ` +
      `${GENERATOR_ENERGIZED_MIN_V}-${GENERATOR_ENERGIZED_MAX_V}V)`
    );
    sanitized.generatorPower = 0;
    sanitized.generatorVoltage = 0;
    if (sanitized.generatorFrequency !== undefined) sanitized.generatorFrequency = 0;
    corrected.push('generatorPower');
  }

  // Battery 2 per-pack power (dual-input HV units). batteryPower is already
  // the summed total; batteryPower2 is telemetry — a corrupt value is
  // DELETED (absent = unknown), never zeroed into a false "0 W" claim.
  if (sanitized.batteryPower2 != null) {
    if (checkRegisterError('batteryPower2', sanitized.batteryPower2)) {
      delete sanitized.batteryPower2;
      corrected.push('batteryPower2');
    } else if (Math.abs(sanitized.batteryPower2) > limits.maxBatteryPowerW) {
      warnings.push(
        `batteryPower2 ${sanitized.batteryPower2}W exceeds max ±${limits.maxBatteryPowerW}W`,
      );
      delete sanitized.batteryPower2;
      corrected.push('batteryPower2');
    }
  }

  // Battery voltage: LV residential packs 0-70V, HV commercial packs 0-1000V
  const maxBatteryVoltage = HV_BATTERY_MODELS.has(modelId) ? 1000 : 70;
  if (data.batteryVoltage > maxBatteryVoltage) {
    warnings.push(`batteryVoltage ${data.batteryVoltage}V exceeds ${maxBatteryVoltage}V — likely register glitch`);
    sanitized.batteryVoltage = 0;
    corrected.push('batteryVoltage');
  }

  // Grid voltage: realistic range 50-500V (anything below 50V on a 120/230V system is garbage)
  if (data.gridVoltage !== undefined && data.gridVoltage > 500) {
    warnings.push(`gridVoltage ${data.gridVoltage}V exceeds 500V`);
    sanitized.gridVoltage = 0;
    corrected.push('gridVoltage');
  }
  if (sanitized.gridVoltage !== undefined && sanitized.gridVoltage > 0 && sanitized.gridVoltage < 50) {
    warnings.push(`gridVoltage ${sanitized.gridVoltage}V below 50V minimum — register misalignment`);
    sanitized.gridVoltage = 0;
    corrected.push('gridVoltage');
  }

  // Phase voltage validation: individual phases below 50V while grid has power = garbage
  if (sanitized.phaseVoltages && Array.isArray(sanitized.phaseVoltages) && sanitized.gridPower !== 0) {
    const hasGarbage = sanitized.phaseVoltages.some((v: number) => v > 0 && v < 50);
    if (hasGarbage) {
      warnings.push(`phaseVoltages [${sanitized.phaseVoltages.join(',')}] contains sub-50V values while grid is active — register misalignment`);
      sanitized.phaseVoltages = sanitized.phaseVoltages.map(() => 0);
      sanitized.gridVoltage = 0;
      corrected.push('phaseVoltages');
    }
  }

  // Phase voltage imbalance guard: a single phase reading far below its healthy
  // siblings is a register glitch, not a real grid event. Three phases of one
  // supply are tightly balanced (real imbalance is a few %, a severe fault ~10% —
  // never 33%), so a phase that contradicts its siblings is provably bogus.
  // Only fires when ≥2 phases are present, so genuine single-phase brownouts
  // (no sibling to contradict) pass through untouched.
  if (sanitized.phaseVoltages && Array.isArray(sanitized.phaseVoltages) && sanitized.phaseVoltages.length >= 2) {
    const PHASE_IMBALANCE_RATIO = 0.75; // outlier if below 75% of the healthy reference
    const MIN_HEALTHY_PHASE_V = 180;    // at least one sibling must show a live voltage
    const phases = sanitized.phaseVoltages as number[];
    const ref = Math.max(...phases);
    if (ref >= MIN_HEALTHY_PHASE_V) {
      const healthy = phases.filter((v) => v >= ref * PHASE_IMBALANCE_RATIO);
      const sortedHealthy = [...healthy].sort((a, b) => a - b);
      const mid = Math.floor(sortedHealthy.length / 2);
      const healthyMedian = sortedHealthy.length % 2 === 0
        ? (sortedHealthy[mid - 1] + sortedHealthy[mid]) / 2
        : sortedHealthy[mid];
      const repaired = phases.map((v) => (v > 0 && v < ref * PHASE_IMBALANCE_RATIO ? healthyMedian : v));
      const changed = repaired.some((v, i) => v !== phases[i]);
      if (changed) {
        warnings.push(`phaseVoltages [${phases.join(',')}] contains a sub-${Math.round(ref * PHASE_IMBALANCE_RATIO)}V outlier while siblings read ~${ref.toFixed(0)}V — register glitch, repaired from healthy phases`);
        sanitized.phaseVoltages = repaired;
        corrected.push('phaseVoltages');
        // gridVoltage mirrors L1 on these inverters; if it shared the glitched value, repair it too.
        if (sanitized.gridVoltage !== undefined && sanitized.gridVoltage < ref * PHASE_IMBALANCE_RATIO) {
          sanitized.gridVoltage = healthyMedian;
          if (!corrected.includes('gridVoltage')) corrected.push('gridVoltage');
        }
      }
    }
  }

  // phaseGridCurrents: inner-side CT amps. Cannot be real without grid voltage.
  if (sanitized.phaseGridCurrents && Array.isArray(sanitized.phaseGridCurrents) && sanitized.phaseGridCurrents.length > 0) {
    const hasVoltage = (Array.isArray(sanitized.phaseVoltages) && sanitized.phaseVoltages.some((v: number) => v > 50)) ||
      (typeof sanitized.gridVoltage === 'number' && sanitized.gridVoltage > 50);
    if (!hasVoltage) {
      warnings.push(`phaseGridCurrents [${sanitized.phaseGridCurrents.join(',')}] present without grid voltage — dropped`);
      sanitized.phaseGridCurrents = [];
      corrected.push('phaseGridCurrents');
    }
  }

  // Guard A — phaseGridPowers sum-vs-total corruption guard.
  //
  // Real hardware incident: phaseGridPowers [2281,2495,2250] (Σ7026 W) while
  // total gridPower was 24 W — a corrupt Modbus read where the per-phase register
  // block belonged to a different frame than the scalar total.
  //
  // Tolerance = max(200 W, |gridPower|) so that normal rounding/timing jitter and
  // small imbalances pass, while multi-kW mismatches are caught. When the per-phase
  // block is flagged, the currents from the same read cycle are also dropped because
  // they are equally unreliable.
  if (Array.isArray(sanitized.phaseGridPowers) && sanitized.phaseGridPowers.length >= 2 &&
      (sanitized.phaseGridPowers as unknown[]).every((v) => typeof v === 'number')) {
    const phaseGridPowerSum = (sanitized.phaseGridPowers as number[]).reduce((a, b) => a + b, 0);
    const gridTotal = sanitized.gridPower ?? 0;
    const guardATolerance = Math.max(200, Math.abs(gridTotal));
    if (Math.abs(phaseGridPowerSum - gridTotal) > guardATolerance) {
      warnings.push(
        `phaseGridPowers [${(sanitized.phaseGridPowers as number[]).join(',')}] sum ${phaseGridPowerSum}W disagrees with total grid ${gridTotal}W — corrupt read, dropped`,
      );
      sanitized.phaseGridPowers = [];
      corrected.push('phaseGridPowers');
      // Same read cycle is suspect — also drop currents if present
      if (Array.isArray(sanitized.phaseGridCurrents) && sanitized.phaseGridCurrents.length > 0) {
        sanitized.phaseGridCurrents = [];
        corrected.push('phaseGridCurrents');
      }
    }
  }

  // Guard B — phaseGridCurrents plausibility cap vs rated inverter power.
  //
  // Real hardware incident: phaseGridCurrents [13.1, 52, 0.1] A — 52 A on one
  // phase is physically impossible for a 12 kW 3-phase inverter (cap ≈ 30 A/phase).
  //
  // capAmps = (nominalAcPowerW / (phaseCount × 200 V)) × 1.5 safety margin.
  // Using 200 V as the conservative voltage floor (well below 230 V nominal) keeps
  // the cap generous enough to never clip a real value while still catching garbled
  // register values like 52 A or 65535 A.
  if (Array.isArray(sanitized.phaseGridCurrents) && sanitized.phaseGridCurrents.length >= 1 &&
      (sanitized.phaseGridCurrents as unknown[]).every((v) => typeof v === 'number')) {
    const phaseCount = (Array.isArray(sanitized.phaseVoltages) && sanitized.phaseVoltages.length > 0)
      ? (sanitized.phaseVoltages as number[]).length
      : 1;
    const nominalW = getNominalAcPowerW(modelId);
    if (nominalW > 0) {
      const capAmps = (nominalW / (phaseCount * 200)) * 1.5;
      if ((sanitized.phaseGridCurrents as number[]).some((a) => Math.abs(a) > capAmps)) {
        warnings.push(
          `phaseGridCurrents [${(sanitized.phaseGridCurrents as number[]).join(',')}] exceeds plausible ${capAmps.toFixed(0)}A/phase for ${modelId} — corrupt, dropped`,
        );
        sanitized.phaseGridCurrents = [];
        corrected.push('phaseGridCurrents');
      }
    }
  }

  // Grid voltage is preserved even when gridPower is 0.
  // Zero power means balanced import/export, not disconnected grid.
  // The grid relay state should be determined by dedicated status registers, not power.

  // Daily energy counters.
  // PV is bounded by the solar heuristic (ratedPower/1000 × 10 peak sun hours).
  // Grid import/export and load are bounded by the grid connection running 24h —
  // households with electric heating legitimately hit 300-400 kWh/day, so they
  // use the flat 500 kWh grid limit instead of the tighter PV heuristic.
  // STRIP, never zero (#53). The cap is a heuristic and it has a proven blind
  // spot: where the gen port carries a micro-inverter, the Deye daily-PV
  // register also accumulates the gen-port feed, so a healthy install crosses
  // it (prod "Home_1", 2 x deye_sg04lp3: own MPPT integral 67.0 kWh + gen-port
  // integral 50.1 kWh = the 117.0 kWh the register reported, against a 120 kWh
  // cap). Writing 0 replaces a suspect number with a CONFIDENT WRONG one: a 0
  // is a legitimate value, so it flows unchallenged into the energy balance the
  // read path derives daily consumption from (pv + import - export + discharge
  // - charge), drives it negative, and clamps that inverter's consumption to
  // nothing — which on a multi-inverter household removes it from the summed
  // total. Absent says "this counter is not trusted", which is what we actually
  // know. Same reasoning, and same shape, as the mirrors-loadPower strip (#47).
  if (data.dailyPvEnergy !== undefined && data.dailyPvEnergy > limits.maxDailyEnergyKwh) {
    warnings.push(`dailyPvEnergy ${data.dailyPvEnergy}kWh exceeds max ${limits.maxDailyEnergyKwh}kWh`);
    delete sanitized.dailyPvEnergy;
    corrected.push('dailyPvEnergy');
  }
  if (data.dailyImportEnergy !== undefined && data.dailyImportEnergy > limits.maxDailyGridEnergyKwh) {
    warnings.push(`dailyImportEnergy ${data.dailyImportEnergy}kWh exceeds max ${limits.maxDailyGridEnergyKwh}kWh`);
    sanitized.dailyImportEnergy = 0;
    corrected.push('dailyImportEnergy');
  }
  if (data.dailyExportEnergy !== undefined && data.dailyExportEnergy > limits.maxDailyGridEnergyKwh) {
    warnings.push(`dailyExportEnergy ${data.dailyExportEnergy}kWh exceeds max ${limits.maxDailyGridEnergyKwh}kWh`);
    sanitized.dailyExportEnergy = 0;
    corrected.push('dailyExportEnergy');
  }
  if (data.dailyLoadEnergy !== undefined && data.dailyLoadEnergy > limits.maxDailyGridEnergyKwh) {
    warnings.push(`dailyLoadEnergy ${data.dailyLoadEnergy}kWh exceeds max ${limits.maxDailyGridEnergyKwh}kWh`);
    sanitized.dailyLoadEnergy = 0;
    corrected.push('dailyLoadEnergy');
  }

  // Unit error: the INSTANTANEOUS load power (W) landing in the daily load
  // ENERGY (kWh) field. Seen on a Home Assistant household whose load-power
  // sensor is wired into the daily-load-energy slot: `dailyLoadEnergy` tracked
  // `loadPower` one-for-one all day (156 W → 155.8 "kWh"), rose and FELL, and
  // fed a ~50x overstatement into the consumption forecast and calendar event
  // outcomes. Signature: the kWh counter equals the watt reading within 2%.
  //
  // Floor of 100 W: below that a numeric match is plausible coincidence (12 W
  // drawn while 12 kWh has been consumed today), so the guard stays out. Above
  // it the mirrored counter would have to be ≥98 kWh, which coinciding with a
  // sub-500 W draw is vanishingly unlikely — and the cost of a rare false
  // strip is one missing counter sample, against a poisoned forecast.
  //
  // Read from `data`, not `sanitized`: the flat 500 kWh cap above may already
  // have zeroed the field, and stripping beats a 0 that the seal's reset
  // detection reads as a midnight counter rollover.
  const MIRROR_MIN_LOAD_W = 100;
  if (
    typeof data.dailyLoadEnergy === 'number' &&
    typeof sanitized.loadPower === 'number' &&
    sanitized.loadPower >= MIRROR_MIN_LOAD_W &&
    Math.abs(data.dailyLoadEnergy - sanitized.loadPower) / sanitized.loadPower < 0.02
  ) {
    warnings.push(
      `dailyLoadEnergy ${data.dailyLoadEnergy}kWh mirrors loadPower ${sanitized.loadPower}W ` +
      `— instantaneous watts stored in the daily energy field, field stripped`,
    );
    delete sanitized.dailyLoadEnergy;
    if (!corrected.includes('dailyLoadEnergy')) corrected.push('dailyLoadEnergy');
  }

  // Solarman stale-response corruption: a mismatched batch reply can land the
  // import counter's value in the dailyPvEnergy field (reg 529/520 on 3-phase,
  // 108/76 on 1-phase are each the first register of two different batches).
  // Signature: dailyPvEnergy EXACTLY equals dailyImportEnergy while PV is idle.
  // Strip (delete, not zero — a 0 late in the day would look like a midnight
  // counter reset to the seal's reset detection).
  if (
    typeof sanitized.dailyPvEnergy === 'number' &&
    typeof sanitized.dailyImportEnergy === 'number' &&
    sanitized.dailyPvEnergy > 0.5 &&
    Math.abs(sanitized.dailyPvEnergy - sanitized.dailyImportEnergy) < 0.101 &&
    (sanitized.pvPower ?? 0) < 50
  ) {
    warnings.push(
      `dailyPvEnergy ${sanitized.dailyPvEnergy} mirrors dailyImportEnergy ${sanitized.dailyImportEnergy} ` +
      `with pvPower≈0 — Solarman cross-batch read corruption, field stripped`,
    );
    delete sanitized.dailyPvEnergy;
    corrected.push('dailyPvEnergy');
  }

  // Cross-field: export can't exceed generation + battery capacity (~15kWh typical).
  // Skip when genPortMode=2 (microinverter / external source on gen port): that
  // production exports via the gen port but is not counted in dailyPvEnergy.
  // Reading value wins (latest signal); fall back to persisted inverter config.
  const effectiveGenPortMode = sanitized.genPortMode ?? inverterData.genPortMode;
  const maxBatteryKwh = 20; // generous upper bound for residential battery
  if (effectiveGenPortMode !== 2 &&
      sanitized.dailyExportEnergy !== undefined && sanitized.dailyPvEnergy !== undefined &&
      sanitized.dailyExportEnergy > sanitized.dailyPvEnergy + maxBatteryKwh + 1) {
    warnings.push(`dailyExportEnergy ${sanitized.dailyExportEnergy}kWh exceeds dailyPvEnergy ${sanitized.dailyPvEnergy}kWh + battery — register misalignment`);
    sanitized.dailyExportEnergy = 0;
    corrected.push('dailyExportEnergy');
  }

  // Timestamp: warn but don't correct (keep original for debugging)
  const now = Date.now();
  const readingTime = new Date(data.timestamp).getTime();
  if (readingTime > now + 5 * 60 * 1000) {
    warnings.push(`timestamp ${data.timestamp} is in the future`);
  }
  if (readingTime < now - 24 * 60 * 60 * 1000) {
    warnings.push(`timestamp ${data.timestamp} is more than 24h old`);
  }

  // Energy balance validation: Sources must approximately equal Sinks
  // Sources: Solar + BatteryDischarge + GridImport + Generator
  // Sinks: Load + BatteryCharge + GridExport
  //
  // IMPORTANT: Inverter readings are NEVER perfectly accurate due to:
  // - Measurement timing differences between sensors (up to 1-2 seconds)
  // - Internal inverter consumption (50-200W typically, not reported)
  // - Rounding in register values (often to nearest 10W)
  // - Sensor calibration drift
  // - High-frequency power fluctuations
  //
  // We ONLY reject readings with PHYSICALLY IMPOSSIBLE data:
  // - Exporting power without any generation source
  // - Load power without any source to provide it
  // - We do NOT reject readings just because they don't balance perfectly
  const batteryDischarge = Math.max(0, -sanitized.batteryPower);
  const gridExport = Math.max(0, -sanitized.gridPower);
  const gridImport = Math.max(0, sanitized.gridPower);
  const generatorPower = sanitized.generatorPower || 0;

  // Total sources that can provide power
  const totalSources = sanitized.pvPower + batteryDischarge + gridImport + generatorPower;

  // Check 1: Exporting more than all sources can provide
  // pvPower ≈ 0 but exporting significantly more than battery + generator
  const impossibleExport = sanitized.pvPower < 50 && gridExport > 500 &&
    gridExport > totalSources + 500;

  // Check 2: Load exceeds all available sources (no-export scenario)
  // User doesn't sell to grid, but load is impossibly high relative to sources.
  // Threshold scales with load size: small loads need looser tolerance (sensor noise),
  // larger loads must balance more tightly (a 480W load with 115W of sources is a real bug).
  // A load that was STRIPPED above (over the model cap) cannot take part in the
  // balance — there is no number left to balance. Treating absent as 0 is what
  // the arithmetic below needs, and it lands on the safe side: `impossibleLoad`
  // requires > 200 W, so a stripped load can never itself trigger a rejection.
  const loadForBalance = sanitized.loadPower ?? 0;
  const loadDeficitThreshold = Math.max(300, 0.6 * loadForBalance);
  const impossibleLoad = gridExport < 100 && loadForBalance > 200 &&
    (loadForBalance - totalSources) > loadDeficitThreshold;

  if ((impossibleExport || impossibleLoad) && !sourcesIncomplete) {
    const batteryCharge = Math.max(0, sanitized.batteryPower);

    if (impossibleExport) {
      const inferredPv = gridExport + loadForBalance + batteryCharge - batteryDischarge - gridImport - generatorPower;
      const reason =
        `energyBalance: pvPower=${sanitized.pvPower}W but exporting ${gridExport}W with only ` +
        `${batteryDischarge}W battery discharge + ${generatorPower}W generator — ` +
        `impossible physics (inferred pvPower ≈ ${Math.round(Math.max(0, inferredPv))}W)`;
      warnings.push(reason);
      energyBalance = {
        side: 'load',
        residualW: gridExport - (batteryDischarge + generatorPower + sanitized.pvPower),
        thresholdW: 500,
        reason,
      };
    } else {
      // impossibleLoad
      const deficit = loadForBalance - totalSources;
      const reason =
        `energyBalance: loadPower=${loadForBalance}W but only ${Math.round(totalSources)}W available ` +
        `(PV:${sanitized.pvPower} + battDischarge:${batteryDischarge} + gridImport:${gridImport} + gen:${generatorPower}) — ` +
        `impossible ${Math.round(deficit)}W deficit`;
      warnings.push(reason);
      energyBalance = {
        side: 'load',
        residualW: deficit,
        thresholdW: loadDeficitThreshold,
        reason,
      };
    }

    // Zero out the reading as it's completely unreliable
    sanitized.pvPower = 0;
    sanitized.gridPower = 0;
    sanitized.batteryPower = 0;
    sanitized.loadPower = 0;
    corrected.push('energyBalance');
  }
  // Check 3: Battery charging without adequate sources
  // Battery cannot charge with more power than available from PV + grid + generator
  const batteryCharge = Math.max(0, sanitized.batteryPower);
  const totalSourcesForCharging = sanitized.pvPower + gridImport + generatorPower;

  // Threshold scales with charge magnitude: a 600W charge with only 115W
  // of sources (PV+grid+gen) is physically impossible — the battery cannot
  // pull energy from nowhere.
  const chargeDeficitThreshold = Math.max(300, 0.6 * batteryCharge);
  const impossibleCharge = batteryCharge > 200 &&
    (batteryCharge - totalSourcesForCharging) > chargeDeficitThreshold;

  if (impossibleCharge && !sourcesIncomplete) {
    const reason =
      `energyBalance: batteryCharge=${batteryCharge}W but only ${Math.round(totalSourcesForCharging)}W available ` +
      `(PV:${sanitized.pvPower} + gridImport:${gridImport} + gen:${generatorPower}) — ` +
      `impossible ${Math.round(batteryCharge - totalSourcesForCharging)}W excess`;
    warnings.push(reason);
    energyBalance = {
      side: 'charge',
      residualW: batteryCharge - totalSourcesForCharging,
      thresholdW: chargeDeficitThreshold,
      reason,
    };
    sanitized.pvPower = 0;
    sanitized.gridPower = 0;
    sanitized.batteryPower = 0;
    sanitized.loadPower = 0;
    corrected.push('energyBalance');
  }

  // The whole reading used to be rejected here whenever ONE mis-signed source
  // field dragged `totalSources` negative — on 2026-09-02 that cost 55 minutes
  // of frames whose SOC, PV, load, voltages and daily counters were all
  // correct, plus three false «Інвертор офлайн» pushes for an inverter that was
  // answering every poll (#477). A hole in the sources is not proof of
  // impossible physics, so say so and store the rest. The all-zero
  // communication-failure check below is unaffected and still rejects.
  if (sourcesIncomplete && (impossibleExport || impossibleLoad || impossibleCharge)) {
    warnings.push(
      'energyBalance not evaluated — a source field was stripped as non-physical, ' +
      'so the source total is incomplete; the reading is stored without it'
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // CATEGORY 4b: All-Zero Communication Failure Detection
  // ═══════════════════════════════════════════════════════════════════════════
  // For battery inverters, all fields being zero simultaneously is physically
  // impossible: BMS cutoff prevents 0% SOC, houses have standby load, and
  // grid voltage is always present. This pattern indicates the harvester
  // failed to read the inverter and sent a zeroed-out reading.
  if (inverterData.hasBattery &&
      sanitized.pvPower === 0 && sanitized.gridPower === 0 &&
      sanitized.batteryPower === 0 && sanitized.loadPower === 0 &&
      sanitized.batterySoc != null && sanitized.batterySoc <= 1) {
    warnings.push(
      `all-zero reading on battery inverter (SOC=${sanitized.batterySoc}%) — likely communication failure ` +
      '(BMS prevents 0% SOC, houses have standby load)'
    );
    corrected.push('communicationFailure');
  }

  // Partial Modbus read: the battery register block read as zero on a battery
  // inverter. A connected battery ALWAYS reports a non-zero pack voltage — BMS
  // cutoff happens well above 0 V, so even at 0% SOC the voltage is tens (LV)
  // or hundreds (HV) of volts. batteryVoltage === 0 together with SOC ≈ 0 means
  // regs 586-591 failed to read. Paired with a zero load block (houses always
  // draw standby load when the inverter is alive), this is a failed/partial
  // read where the few non-zero fields carry stale or garbage values. The
  // all-zero check above misses it because those garbage fields are non-zero.
  // (magdich.igor 2026-05-29: a frame with battery=0, load=0 but pv=42 /
  // grid=1220 / gen=1482 and gridRelayClosed=false flipped a healthy inverter
  // to a false "outage" / "Standby 0%". Distinct from a dead-but-connected
  // battery, which still reports voltage and a non-zero load — see the
  // "non-zero power but zero SOC" test case.)
  if (inverterData.hasBattery &&
      sanitized.batteryVoltage === 0 &&
      sanitized.batterySoc != null && sanitized.batterySoc <= 1 &&
      sanitized.loadPower === 0 &&
      !corrected.includes('communicationFailure')) {
    warnings.push(
      `battery register block read as zero (batteryVoltage=0V, SOC=${sanitized.batterySoc}%) ` +
      'with zero load — likely partial/failed Modbus read (a connected battery always has non-zero voltage)'
    );
    corrected.push('communicationFailure');
  }

  // Implausibly-low battery voltage on an LV (48V-class) pack → corrupt frame.
  //
  // shura.nik73 "Дача" (deye_sg05lp1 edge, fw 2.89.1, 2026-07-04): Solarman
  // batch-read contamination spliced another batch's registers into the battery
  // slot, so batteryVoltage flapped 0 / 11.5 / 23 / 34 V every other 5-min
  // sample while SOC stayed ~100%. A 16S LiFePO4 (all our LV hybrids are 48V-
  // class, ~51.2V nominal) is NEVER below ~40V while the inverter reads it — BMS
  // cutoff sits ~40-44V, and below that the pack disconnects and reads a clean 0
  // (covered by the zero-block guard above). So any 0 < batteryVoltage < 40V on
  // an LV pack proves the whole frame is corrupt (its powers are garbage too —
  // the same read carried impossible pvPower spikes). Reject the reading.
  //
  // Scoped to LV: HV commercial packs (hundreds of volts) have their own
  // corruption profile, caught by the zero-block and proportional guards.
  //
  // ⚠️ The floor SCALES WITH THE PACK. "All our LV hybrids are 48V-class" was
  // never true: `srne_hsi_1500` is 12 V (see its catalogue note above) and the
  // Anenji ANJ-4.2KW-24V-W-Pro is 24 V (nameplate: 24 VDC input, 27 VDC charger
  // output). A HEALTHY pack on either sits inside a fixed 40 V band, so a
  // hard-coded floor rejects every reading they ever produce — and
  // `communicationFailure` makes the MQTT consumer DROP the reading, so the
  // owner sees an empty chart with nothing explaining it.
  //
  // The OWNER'S configured pack wins over the model table when both chemistry
  // and cell count are set. One model id can cover several pack voltages:
  // `anenji_anj_4200` is the SMG II entry, its map is picked at runtime, and a
  // 12 V ANJ-2000W reports through it (Cotești, 2026-10-14). The table's 24 V
  // put that pack's floor at 20 V and dropped every reading it made. A 16S
  // LiFePO4 pack still lands on exactly 40 V (16 x 2.5 V), the original floor.
  const nominalV = LV_PACK_NOMINAL_V[modelId] ?? 48;
  // The same ratio the 48 V case has always used (40 / 48), so the original
  // incident behaves identically and smaller packs get a floor that means the
  // same thing: "below this the BMS has cut off and the pack reads a clean 0".
  const configuredFloorV = configuredPackFloorV(inverterData.batteryChemistry, inverterData.batteryCellCount);
  const LV_PACK_ALIVE_MIN_V = configuredFloorV ?? Math.round(nominalV * (40 / 48) * 10) / 10;
  const packLabel = configuredFloorV !== null
    ? `configured ${inverterData.batteryCellCount}S pack`
    : `${nominalV}V-class LV pack`;
  if (inverterData.hasBattery &&
      !HV_BATTERY_MODELS.has(modelId) &&
      data.batteryVoltage > 0 && data.batteryVoltage < LV_PACK_ALIVE_MIN_V &&
      !corrected.includes('communicationFailure')) {
    warnings.push(
      `battery voltage ${data.batteryVoltage}V is implausibly low for a live ` +
      `${packLabel} (min ~${LV_PACK_ALIVE_MIN_V}V before BMS cutoff) — ` +
      'corrupt frame, register block contaminated by another Modbus batch'
    );
    corrected.push('communicationFailure');
  }

  // SOC-vs-voltage contradiction → corrupt frame (flag-gated, re-introduced
  // 2026-07-18). A flaky logger can read the voltage register block ~0.8x low
  // while the SOC register stays intact, so battery voltage lands well below what
  // its (high) SOC implies — physically impossible. Uses the CONFIGURED cell
  // count + resolved chemistry and fails open when either is missing/unknown.
  //
  // Unlike the earlier version (reverted after the 2026-07-18 incident), this
  // does NOT mark the whole reading `communicationFailure` — that flag makes the
  // MQTT consumer DROP the reading (stale live view). Instead we drop ONLY the
  // impossible voltage (a field-level correction) and KEEP the reading, so the
  // live view stays fresh and the two ingest paths behave identically. Gated by
  // `batteryCorruptGuardEnabled` (default off) for staged rollout + kill-switch.
  if (inverterData.batteryCorruptGuardEnabled &&
      inverterData.hasBattery &&
      typeof sanitized.batteryVoltage === 'number' &&
      batteryReadingContradictsSoc({
        batteryVoltage: data.batteryVoltage,
        batterySoc: data.batterySoc,
        chemistry: inverterData.batteryChemistry,
        cellCount: inverterData.batteryCellCount,
      })) {
    warnings.push(
      `battery voltage ${data.batteryVoltage}V is impossible for SOC ${data.batterySoc}% ` +
      '(near empty at high charge) — corrupt frame, voltage dropped'
    );
    (sanitized as Record<string, unknown>).batteryVoltage = null;
    corrected.push('batteryVoltage');
  }

  // Proportional corrupt-frame detection (SOC + voltage scaled by same factor).
  // A flaky logger can return a frame where the whole register block is read with
  // a corrupted scaling factor, so SOC, battery voltage AND the powers all come
  // back scaled DOWN by the same fraction (e.g. SOC 10→6 alongside pack voltage
  // 613→368, both ×0.60). The SOC-vs-voltage cross-check above can't catch this
  // because the two agree. The tell is physical: a real discharge moves SOC while
  // terminal voltage stays nearly flat (Li/LFP voltage is flat across most of the
  // SOC range), so SOC and voltage only fall by the SAME fraction on a corrupt
  // frame. Reject the whole reading — its powers are garbage too.
  // (Lemberg deye_sg01hp3_50k 2026-06-05: corrupt frames produced a false
  // "Critical Battery 6%" while the pack actually held at its 10% reserve.)
  const prevSocVal = inverterData.prevBatterySoc;
  const prevVoltVal = inverterData.prevBatteryVoltage;
  const VOLTAGE_SOC_FRACTION_TOLERANCE = 0.10; // SOC & V must fall within 10pts of the same fraction
  const MIN_PROPORTIONAL_DROP = 0.10;          // ignore small proportional noise
  if (inverterData.hasBattery &&
      typeof prevSocVal === 'number' && prevSocVal > 0 &&
      typeof prevVoltVal === 'number' && prevVoltVal > 0 &&
      data.batterySoc != null && data.batterySoc > 0 && data.batterySoc < prevSocVal &&
      data.batteryVoltage > 0 && data.batteryVoltage < prevVoltVal &&
      !corrected.includes('communicationFailure')) {
    const socFraction = data.batterySoc / prevSocVal;
    const voltageFraction = data.batteryVoltage / prevVoltVal;
    const voltageDrop = 1 - voltageFraction;
    if (voltageDrop >= MIN_PROPORTIONAL_DROP &&
        Math.abs(voltageFraction - socFraction) <= VOLTAGE_SOC_FRACTION_TOLERANCE) {
      warnings.push(
        `corrupt frame: battery SOC ${prevSocVal}→${data.batterySoc}% and voltage ` +
        `${prevVoltVal}→${data.batteryVoltage}V fell by the same proportion ` +
        `(SOC ×${socFraction.toFixed(2)}, V ×${voltageFraction.toFixed(2)}) — whole register ` +
        `block scaled by a garbage factor, not a real discharge`
      );
      corrected.push('communicationFailure');
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // CATEGORY 5: Battery SOC vs Power Direction (temporal consistency)
  // ═══════════════════════════════════════════════════════════════════════════

  // SOC at extreme with contradictory power flow
  // SOC = 100% but still charging significantly (BMS would limit this)
  if (sanitized.batterySoc != null && sanitized.batterySoc >= 100 && sanitized.batteryPower > 500) {
    warnings.push(
      `batterySoc=100% but batteryPower=${sanitized.batteryPower}W (charging) — ` +
      `BMS should limit charge at full SOC`
    );
    // Don't zero out - this might just be BMS reporting lag, but flag it
  }

  // SOC = 0% but still discharging significantly (BMS would cut off)
  if (sanitized.batterySoc != null && sanitized.batterySoc <= 0 && sanitized.batteryPower < -500) {
    warnings.push(
      `batterySoc=0% but batteryPower=${sanitized.batteryPower}W (discharging) — ` +
      `BMS should prevent discharge at empty SOC`
    );
    // Don't zero out - flag for investigation
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // CATEGORY 6: Daily Counter Anomalies
  // ═══════════════════════════════════════════════════════════════════════════

  // genPortMode is a 3-state enum (0=generator, 1=smart load, 2=microinverter).
  // Out-of-range values come from misaligned register reads; drop them so
  // downstream consumers don't render garbage.
  if (sanitized.genPortMode !== undefined &&
      (sanitized.genPortMode < 0 ||
       sanitized.genPortMode > 2 ||
       !Number.isInteger(sanitized.genPortMode))) {
    warnings.push(`genPortMode ${sanitized.genPortMode} is out of range [0..2]`);
    // Delete (not assign undefined): Firestore rejects docs containing
    // `undefined`, so leaving the key present crashes the ingest .set() with
    // `Cannot use "undefined" as a Firestore value`.
    delete sanitized.genPortMode;
    corrected.push('genPortMode');
  }

  // Daily counters should never be negative
  if (data.dailyPvEnergy !== undefined && data.dailyPvEnergy < 0) {
    warnings.push(`dailyPvEnergy ${data.dailyPvEnergy}kWh is negative`);
    sanitized.dailyPvEnergy = 0;
    corrected.push('dailyPvEnergy');
  }
  if (data.dailyImportEnergy !== undefined && data.dailyImportEnergy < 0) {
    warnings.push(`dailyImportEnergy ${data.dailyImportEnergy}kWh is negative`);
    sanitized.dailyImportEnergy = 0;
    corrected.push('dailyImportEnergy');
  }
  if (data.dailyExportEnergy !== undefined && data.dailyExportEnergy < 0) {
    warnings.push(`dailyExportEnergy ${data.dailyExportEnergy}kWh is negative`);
    sanitized.dailyExportEnergy = 0;
    corrected.push('dailyExportEnergy');
  }
  if (data.dailyLoadEnergy !== undefined && data.dailyLoadEnergy < 0) {
    warnings.push(`dailyLoadEnergy ${data.dailyLoadEnergy}kWh is negative`);
    sanitized.dailyLoadEnergy = 0;
    corrected.push('dailyLoadEnergy');
  }

  // Note: We deliberately do NOT warn on imbalanced readings because inverter
  // measurements are inherently inaccurate. Only impossible readings are rejected.

  return {
    valid: warnings.length === 0,
    warnings,
    corrected,
    energyBalance,
    sanitizedData: sanitized,
  };
}

/**
 * Number of consecutive corroborating reads required before a stuck-low daily
 * counter is allowed to resync UP to a higher (in-bounds) value. Mirrors the
 * `>= 3` threshold used for the downward consecutive-drop spike correction.
 */
const DAILY_COUNTER_RISE_CONFIRMATIONS = 3;

/**
 * Determine the plausible daily energy value given cross-source validation,
 * consecutive-drop spike correction (stuck-HIGH), and consecutive-rise catch-up
 * (stuck-LOW).
 *
 * `maxDailyKwh` (the model's physical daily-energy ceiling) is required to
 * enable the stuck-low catch-up: it bounds what counts as a plausible resync so
 * genuine garbage spikes (e.g. a register read of 499 kWh on a ~9 kWh day) are
 * still rejected. Callers that omit it keep the legacy "reject implausible
 * upward jump" behaviour.
 */
export function plausibleDailyEnergyWithSource(params: {
  field: string;
  newVal: number | undefined;
  existingMax: number;
  lastReadingVal: number;
  maxJumpKwh: number;
  readingSource: string;
  existingSource: string | undefined;
  dropCount: number;
  riseCount?: number;
  maxDailyKwh?: number;
}): { value: number; source: string | undefined; dropCount: number; riseCount: number } {
  const { newVal, existingMax, lastReadingVal, maxJumpKwh, readingSource, existingSource, dropCount, maxDailyKwh } = params;
  const riseCount = params.riseCount ?? 0;

  // No new value — keep existing (preserve any in-flight catch-up streak)
  if (newVal === undefined) {
    return { value: existingMax, source: existingSource, dropCount, riseCount };
  }

  // Midnight reset: new value is small and less than last — keep existing max,
  // clear the catch-up streak (the counter is starting a fresh day)
  if (newVal < lastReadingVal) {
    return { value: existingMax, source: existingSource, dropCount, riseCount: 0 };
  }

  const jump = newVal - lastReadingVal;
  const sameSource = !existingSource || readingSource === existingSource;

  if (sameSource) {
    // Same source: normal monotonic logic
    if (jump <= maxJumpKwh) {
      const accepted = Math.max(existingMax, newVal);
      // Check for consecutive drop: value significantly below stored max
      if (existingMax > 0 && newVal < existingMax * 0.6) {
        const newDropCount = dropCount + 1;
        if (newDropCount >= 3) {
          // Spike correction: stored max was likely corrupted
          return { value: newVal, source: readingSource, dropCount: 0, riseCount: 0 };
        }
        return { value: existingMax, source: existingSource, dropCount: newDropCount, riseCount: 0 };
      }
      return { value: accepted, source: readingSource, dropCount: 0, riseCount: 0 };
    }

    // Implausible upward jump from same source. This is either a one-off
    // garbage spike OR the counter catching up after a stale-low read locked
    // it (naffilet999 2026-05-25: dailyPvEnergy frozen at 6.4 while the inverter
    // climbed to 47.3, every catch-up read rejected as a spike). Distinguish by
    // corroboration across consecutive reads, bounded by the physical daily
    // ceiling: a transient spike won't persist; a real catch-up will.
    if (newVal > existingMax && maxDailyKwh !== undefined && newVal <= maxDailyKwh) {
      const newRiseCount = riseCount + 1;
      if (newRiseCount >= DAILY_COUNTER_RISE_CONFIRMATIONS) {
        return { value: newVal, source: readingSource, dropCount: 0, riseCount: 0 };
      }
      return { value: existingMax, source: existingSource, dropCount, riseCount: newRiseCount };
    }

    // Over the daily ceiling, or no ceiling supplied — reject as a spike and
    // clear the catch-up streak.
    return { value: existingMax, source: existingSource, dropCount, riseCount: 0 };
  }

  // Different source: only accept if jump is plausible
  if (newVal > existingMax) {
    if (jump <= maxJumpKwh) {
      return { value: newVal, source: readingSource, dropCount: 0, riseCount: 0 };
    }
    // Implausible cross-source jump — reject
    return { value: existingMax, source: existingSource, dropCount, riseCount: 0 };
  }

  // Different source, lower value — ignore
  return { value: existingMax, source: existingSource, dropCount, riseCount: 0 };
}

/**
 * Pre-filter for daily-reset energy counters before they feed the 5-min bucket
 * accumulator. A spurious register read (e.g. a misaligned Modbus batch) spikes
 * far above the previous value; the bucket's max-of-bucket then latches it, and
 * the hourly→daily→monthly rollups inherit the corruption (naffilet999,
 * 2026-05: dailyGeneratorEnergy read 499 amid a ~9 kWh day → monthly 2420 kWh).
 *
 * Clamps an implausible UPWARD jump back to the previous value. Downward moves
 * (midnight reset, register noise) pass through untouched — the bucket must
 * track the live counter, including resets, unlike the daily-doc/current path
 * which keeps the day's max via `plausibleDailyEnergyWithSource`.
 */
export function clampDailyCounterSpikeForBucket(
  newVal: number | undefined,
  prevVal: number | undefined,
  maxJumpKwh: number,
): number | undefined {
  if (typeof newVal !== 'number') return newVal;
  if (typeof prevVal !== 'number') return newVal;
  return newVal > prevVal + maxJumpKwh ? prevVal : newVal;
}

/**
 * Grid-power gate for a daily grid-energy counter (import OR export).
 *
 * A daily grid counter can only legitimately INCREASE while power is actually
 * flowing that way — export while grid power is negative, import while it is
 * positive (per the codebase convention `exportEnergy = max(0, -gridPower)`,
 * `importEnergy = max(0, gridPower)`).
 *
 * Some Solarman daily counters emit phantom values — both sudden spikes AND
 * gradual ramps — while grid power shows no such flow: reg 77 phantom EXPORT on
 * 1-phase Deye at grid=0 (nikolaj.povzlo); reg 520 phantom IMPORT on 3-phase
 * Deye while the inverter is EXPORTING (Stepan Martsyniak). Gradual ramps defeat
 * `clampDailyCounterSpikeForBucket` (each step is within maxJump), and spikes
 * that follow an absent reading bypass it (prevVal undefined). This gate is
 * independent of jump size: whenever live grid power doesn't confirm the
 * counter's direction, it holds the counter at its previous value so phantom
 * increments cannot accumulate. Genuine flow (grid power clearly in the matching
 * direction, e.g. Garage SG05LP3 export at -7 kW) passes through. Downward moves
 * (midnight reset, register noise) also pass through. (2026-05-25)
 *
 * @param newVal     incoming daily counter (kWh) for this reading
 * @param prevVal    previous reading's value (kWh); undefined → 0
 * @param gridPower  instantaneous grid power (W); negative = exporting, positive = importing
 * @param direction  which counter this is — gates against the matching flow sign
 * @param thresholdW grid power magnitude that counts as real flow (default 50 W, ignores noise)
 */
export function gateDailyGridCounterByPower(
  newVal: number | undefined,
  prevVal: number | undefined,
  gridPower: number | undefined,
  direction: 'import' | 'export',
  thresholdW = 50,
): number | undefined {
  if (typeof newVal !== 'number') return newVal;
  const prev = typeof prevVal === 'number' ? prevVal : 0;
  const flowing = typeof gridPower === 'number' && (
    direction === 'export' ? gridPower < -thresholdW : gridPower > thresholdW
  );
  if (flowing) return newVal;
  // No confirming flow: the counter must not increase. Hold at prev; let
  // equal/lower values (steady real total, midnight reset) pass through.
  return newVal > prev ? prev : newVal;
}

/**
 * Backfill repair for an already-written daily `dailyGeneratorEnergy` value
 * (gen-port register 536, shared by generator / smart-load / micro modes).
 *
 * The going-forward ingest clamp (`clampDailyCounterSpikeForBucket`) prevents
 * NEW spikes, but daily docs written before it shipped still carry corrupt
 * values — sporadic spurious register reads latched single-day totals like
 * 485 / 350 / 266 kWh (ivanon2 RubHome, 2026-05). Summed day-by-day into
 * `readings_monthly.generatorEnergy`, these balloon the month past the whole-
 * house load and break the dashboard house-load subtraction.
 *
 * A value is implausible when it is large AND either:
 *   (a) exceeds the model's physical daily cap (`capKwh` = ratedPowerW/1000*10), or
 *   (b) dwarfs the day's own load energy (`> ratio ×` loadEnergy) — the gen port
 *       feeds INTO the load, so its daily energy can't be many times the load.
 *
 * The `> floorKwh` guard keeps small genuine values (early-morning slivers,
 * low-load partial days where gen modestly exceeds load) untouched. Flagged
 * days fall back to the decomposed, power-integrated `smartLoadKwh` (the
 * physical best-estimate), or 0 when that is unavailable.
 *
 * Pure + side-effect-free so the backfill script and its tests share it.
 */
export function repairImplausibleDailyGeneratorEnergy(opts: {
  generatorEnergy: number | undefined;
  loadEnergy: number | undefined;
  smartLoadKwh: number | undefined;
  capKwh: number;
  ratio?: number;
  floorKwh?: number;
}): number | undefined {
  const { generatorEnergy, loadEnergy, smartLoadKwh, capKwh } = opts;
  const ratio = opts.ratio ?? 5;
  const floorKwh = opts.floorKwh ?? 30;
  if (typeof generatorEnergy !== 'number') return generatorEnergy;
  if (generatorEnergy <= floorKwh) return generatorEnergy;
  const load = typeof loadEnergy === 'number' ? loadEnergy : 0;
  const exceedsCap = generatorEnergy > capKwh;
  const dwarfsLoad = generatorEnergy > ratio * load;
  if (exceedsCap || dwarfsLoad) {
    return typeof smartLoadKwh === 'number' ? smartLoadKwh : 0;
  }
  return generatorEnergy;
}
