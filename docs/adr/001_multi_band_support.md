# ADR-001: Multi-Band Price-Band Optimisation for Scheduling Solver

## Status

Proposed

## Context

The scheduling solver currently collapses all MW to a single price band (the cheapest), even though the Modelica model already declares `fcr_capacity_deltas[n_fcr_bands]`, `afrr_up_capacity_deltas[n_afrr_up_bands]`, and `afrr_down_capacity_deltas[n_afrr_down_bands]` arrays. True multi-band optimisation — where the solver distributes MW across ascending price levels to maximise expected revenue under clearing-price uncertainty — is not implemented.
This is problematic for pay-as-bid markets like German aFRR capacity bidding. because a minimum price of 0 gets bid, resulting in 0 * volume = 0 EUR revenue.

The backtesting platform sends multi-band market configurations (`n_price_bands`, `bid_offer_prices[]`, `offer_prices[]`) that are currently ignored and documented in `_info` as unsupported. This limits the quality of day-ahead scheduling decisions: a deterministic single-price model cannot capture the marginal benefit of bidding incrementally at different price levels.

Additionally, the day-ahead market currently lacks the equivalent of the reserve band arrays. There are no `da_power_in_deltas` or `da_power_out_deltas` Modelica variables; `charge_power` and `discharge_power` are free variables rather than derived quantities.

### Limitations of Current Architecture:

1. **No day-ahead band decomposition** — `charge_power`/`discharge_power` are scalar free variables, not sums of per-band deltas.
2. **No price uncertainty model** — the objective is purely deterministic (`net_power * price`), ignoring clearing-price distribution.
3. **Reserve bands unused** — although the Modelica arrays exist, the solver collapses everything to band 1.
4. **Scheduling model-dir assumed fixed** — Decision 7 only caches the intraday model per `n_segments`. The scheduling model directory is permanent and unpatched, blocking parameterised `n_da_bands`.
5. **No support for pay-as-bid markets** - Price is always minimum, which means 0 in case of capacity markets.

## Decision

Extend the scheduling solver to support true multi-band price-band optimisation across four architectural dimensions:

### 1. Modelica Model Extension (BESS.mo)

Add `n_da_bands` parameter and `da_power_in_deltas[n_da_bands]`, `da_power_out_deltas[n_da_bands]` arrays to `BESS.mo`. Redefine `charge_power` and `discharge_power` as derived quantities:

```
charge_power = sum(da_power_in_deltas)
discharge_power = sum(da_power_out_deltas)
```

This mirrors the established pattern used by reserve arrays (`fcr_capacity_deltas[n_fcr_bands]`).

### 2. Expected-Value Objective Under Price Uncertainty

Replace the deterministic objective with an expected-value formulation:

- **Day-ahead (marginal clearing):** `E[revenue] = sum_k(P(clear >= price[k]) * price[k] * delta_out[k]) - sum_k(P(clear <= price[k]) * price[k] * delta_in[k])`
- **FCR/aFRR capacity (pay-as-bid):** `E[standby_revenue] = sum_k(P(accepted at offer_price[k]) * offer_price[k] * delta[k])`

Clearing probabilities are pre-computed constants (not decision variables), preserving the MILP structure and linear objective.

### 3. New Translation Layer Component

A new module `service/translation/clearing_probability.py` computes clearing/acceptance probabilities:

- **Ensemble mode:** Empirical CDF from ensemble timeseries (`P(clear >= X) = fraction of members with price >= X`)
- **Fallback mode:** Normal distribution with `mean=forecast`, `std=forecast * confidence_pct / 200`, floored at 1.0 EUR/MWh

### 4. Scheduling Model-Dir Caching (extends Decision 7)

Adopt the same stable-dir-per-config pattern as intraday: cache keyed on `n_da_bands` with write-lock for thread safety. When `n_da_bands=1`, the original repo directory is used unchanged.

### 5. Wire Format Extensions

**New optional inputs:** `day_ahead_price_ensemble[1..M]`, `fcr_standby_price_ensemble[1..M]`, `afrr_up_standby_price_ensemble[1..M]`, `afrr_down_standby_price_ensemble[1..M]`.

**Market config extension:** `confidence_pct` field (default 10.0) for Normal fallback.

**New outputs:** `day_ahead_power_in_deltas[1..N]`, `day_ahead_power_out_deltas[1..N]`. Existing aggregate `day_ahead_power_in`/`day_ahead_power_out` retained for backward compatibility.

### 6. Per-Request Probability Injection (extends Decision 2)

Pre-computed probability arrays are injected as class attributes on the dynamic per-request solver class, alongside existing `_cycling_penalty`, `_reserve_config`, etc.

## Consequences

### Positive

- **Revenue optimisation under uncertainty** — the solver can now distribute bids across price levels to maximise expected clearing revenue, capturing the full value of price volatility.
- **True multi-band reserve bidding** — reserves use actual per-band solver output instead of the "collapse to band 1" approximation, improving fidelity with the backtesting platform's expectations.
- **Ensemble-aware decisions** — when the orchestrator provides price ensembles, the solver exploits distributional information rather than a single point forecast.
- **Graceful degradation** — Normal fallback engages automatically when ensembles are unavailable; `n_bands=1` reproduces current behaviour identically.
- **No structural MILP change** — probabilities are constants, so the objective remains linear and the solver engine (HiGHS) requires no changes.
- **Removes multiple `_info` warnings** — eliminates "multi-band pricing not supported", "collapsed to band 1", and "market config ignored" entries.

### Negative

- **Increased solver complexity** — the objective function grows from a simple `net_power * price` to a sum over bands with probability weights. Debugging solver output requires understanding the expected-value formulation.
- **Scheduling model-dir caching adds operational surface** — a new cache dimension (keyed on `n_da_bands`) means additional temp directories to manage and potential cold-start latency for novel `n_da_bands` values.
- **Modelica model becomes parametrically sized** — `BESS.mo` gains a dimension parameter (`n_da_bands`) that must be patched per request, mirroring the complexity already present in intraday's `n_segments`.
- **Translation layer grows** — new `clearing_probability.py` module, ensemble parsing logic, and probability-to-class-attribute wiring increase the translation layer's footprint.
- **Testing matrix expands** — need coverage for: ensemble mode, fallback mode, `n_bands=1` regression, various `n_bands` values, edge cases (forecast=0, empty ensemble).
- **`charge_power`/`discharge_power` become derived** — any future feature that directly bounds these must now work through the sum-of-deltas structure.

### Neutral

- **No new external dependencies** — `scipy.stats.norm` is already available transitively. No license or supply-chain impact.
- **Intraday solver unchanged** — reserves remain committed-only in intraday; multi-band bidding is scheduling-only.
- **MILP solve time impact negligible** — N=10 bands x 96 PTUs = 960 new continuous variables is small relative to the existing binary variable count.
- **Physics stays in Modelica; economics stays in Python** — the fundamental separation of concerns is preserved.
- **`_info` transparency contract maintained** — new `applied:` and `approximation:` entries replace the current `ignored_input:` and `not_in_output:` entries.

## Affected Modules

| Module | Impact | Migration needed |
|---|---|---|
| `scheduling/model/BESS.mo` | Add `n_da_bands` parameter, DA delta arrays; redefine `charge_power`/`discharge_power` as derived | Yes — pymoca cache must be cleared |
| `scheduling/src/bess.py` | Rewrite `path_objective()` for expected-value multi-band; extend `constraints()` for per-band block-equality | Yes |
| `service/translation/clearing_probability.py` | **New module** — probability computation logic | N/A (new file) |
| `service/translation/pe_to_rtc.py` | Parse ensemble timeseries, compute probabilities, populate `TranslationResult` with band config | Yes |
| `service/translation/rtc_to_pe.py` | Read per-band DA columns, emit delta members, remove "collapse to band 1" logic | Yes |
| `service/solver_runner.py` | Add scheduling model-dir cache keyed on `n_da_bands`; pass probability arrays to dynamic class | Yes |
| `service/solvers/scheduling.py` | Accept probability arrays and band config as class attributes | Yes |
| `continuous_intraday/model/BESSIntraday.mo` | No change | No |
| `service/solvers/intraday.py` | No change | No |
| `docs/ARCHITECTURE.md` | Sections 7 and 8 require updates to document multi-band and model-dir caching | Yes (after ADR accepted) |

## Migration Steps

1. **Phase 1 — Modelica model extension**: Add `n_da_bands`, `da_power_in_deltas[]`, `da_power_out_deltas[]` to `BESS.mo`. Derive `charge_power`/`discharge_power` from sums. Clear pymoca cache. Verify existing single-band tests still pass (`n_da_bands=1` default).

2. **Phase 2 — Clearing probability module**: Create `service/translation/clearing_probability.py` with `compute_clearing_probabilities()` and `compute_acceptance_probabilities()`. Add unit tests covering ensemble mode, Normal fallback, edge cases (forecast=0, single member).

3. **Phase 3 — Objective function extension**: Rewrite `path_objective()` in `bess.py` to use expected-value formulation with probability-weighted band revenue. Accept probability arrays from class attributes. Add per-band block-equality constraints.

4. **Phase 4 — Translation layer integration**: Update `pe_to_rtc.py` to parse ensemble timeseries, call probability computation, populate `TranslationResult`. Update `rtc_to_pe.py` to emit per-band DA output members and remove "collapse to band 1" logic.

5. **Phase 5 — Solver runner and model-dir caching**: Implement scheduling model-dir cache in `solver_runner.py` keyed on `n_da_bands`. Wire probability arrays to dynamic solver class attributes.

6. **Phase 6 — Wire format and `_info`**: Update output translation for new members. Remove obsolete `_info` entries; add new `applied:`/`approximation:` entries.

7. **Phase 7 — Backward compatibility verification**: Run full test suite with `n_price_bands=1` (or absent) to confirm zero regression. Add integration tests with multi-band market config.

8. **Phase 8 — Architecture documentation**: Update `docs/ARCHITECTURE.md` sections 7 (Compilation Cache) and 8 (Reserve Market Integration) to document the multi-band model and scheduling model-dir caching.

## Risks and Unknowns

| Risk | Severity | Mitigation |
|---|---|---|
| pymoca cache invalidation when `n_da_bands` changes | Low | Stable-dir-per-config pattern (proven in intraday). First request for new N recompiles; subsequent reuse cache. |
| Forecast=0 edge case causing degenerate probability distribution | Low | Floor std at 1.0 EUR/MWh. Solver naturally idles when all band probabilities are ~0. |
| MILP solve time increase | Low | 960 new continuous variables is negligible vs existing binary count. Monitor with OpenTelemetry timer spans. |
| Block-equality constraint explosion for many bands x many blocks | Medium | N_bands typically <= 10. Constraint count grows linearly with bands x blocks — monitor but unlikely to be binding. |
| Interaction with counterfactual reserve re-solve (Decision 9) | Low | Counterfactual strips reserve inputs; DA bands remain and use same probability arrays. No conflict. |
| `charge_power`/`discharge_power` as derived quantities may affect future constraints | Medium | Any constraint on aggregate power still works through the sum. Document this structural change clearly. |
| Ensemble data quality (few members, biased) | Medium | Not in scope of this ADR — probability quality is the orchestrator's responsibility. The solver is correct given any valid probability input. |

## References

- `.ai/plans/multi_band_support.md` — Detailed implementation plan
- `docs/ARCHITECTURE.md` Section 7 — Compilation Cache Strategy (extended by this ADR)
- `docs/ARCHITECTURE.md` Section 8 — Reserve Market Integration (extended by this ADR)
- `docs/ARCHITECTURE.md` Decision 2 — Dynamic Class-per-Request (extended by this ADR)
