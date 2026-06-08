# Coding Guidelines

Project-specific coding conventions for the Backtesting Platform. These complement (and do not duplicate) the base coding rules enforced by the development tooling.

---

## Import Organization

**Rule: All imports must be at the top of the file.**

Do not use local/inline imports in the middle of functions or methods. This applies even when an import is only used in one place.

```python
# CORRECT - imports at top of file
from src.core.dst import validate_ptu_alignment, get_local_date

def place_order(self, delivery_start: int, ...) -> dict:
    validate_ptu_alignment(delivery_start, delivery_end, self._config.interval_seconds)
    delivery_date = get_local_date(delivery_start)
    ...
```

```python
# WRONG - inline import
def place_order(self, delivery_start: int, ...) -> dict:
    from src.core.dst import validate_ptu_alignment  # Don't do this
    validate_ptu_alignment(...)
```

**Rationale**: 
- Makes dependencies explicit and visible at file level
- Enables tooling (linters, IDEs) to detect unused imports
- Avoids import-time surprises during function execution
- Standard Python convention (PEP 8)

---

## Comments

**Rule: Comments explain "why", not "what".**

Good comments explain reasoning, trade-offs, or non-obvious decisions. Do not write comments that merely restate what the code does.

```python
# CORRECT - explains why
# Settlement must happen after positions are recorded to avoid race condition
self._imbalance_market.advance_to(self._current_time)

# CORRECT - explains non-obvious business rule
# FCR is symmetric: capacity must be available in both directions
if abs(bid_up) != abs(bid_down):
    raise ValueError("FCR bids must be symmetric")
```

```python
# WRONG - restates the code
# Advance the imbalance market to current time
self._imbalance_market.advance_to(self._current_time)

# WRONG - repeats function name
def calculate_settlement_cost(self):
    """Calculate the settlement cost."""  # Just repeating the name
    ...

# WRONG - describes obvious logic
# Check if volume is zero
if volume == 0:
    raise ValueError("Volume cannot be zero")
```

**Guidelines**:

| Do | Don't |
|----|-------|
| Explain business rules and domain knowledge | Describe what the code literally does |
| Document non-obvious algorithms or formulas | Repeat function/variable names |
| Note workarounds or technical debt | State the obvious |
| Reference external specs or regulations | Write comments for every line |
| Explain "why" a particular approach was chosen | Describe "what" when the code is self-explanatory |

**Docstrings**: Function/method docstrings should describe the contract (what it does, parameters, returns, raises) but not implementation details. The docstring answers "what does this function do?" while inline comments answer "why is it done this way?"

---

## Type Aliases

TBD

## Unit Suffix Convention

Numeric fields must carry their physical unit as a suffix. This is the primary mechanism for preventing unit confusion — there is no type-system enforcement.

| Suffix | Unit | Examples |
|--------|------|---------|
| `_kw` | Power (kilowatts) | `power_kw`, `max_charge_power_kw`, `flow_kw`, `physical_capacity_kw` |
| `_mw` | Power (megawatts) | `volume_mw`, `flow_mw`, `capacity_mw` |
| `_kwh` | Energy (kilowatt-hours) | `soc_kwh`, `capacity_kwh`, `energy_kwh` |
| `_mwh` | Energy (megawatt-hours) | `energy_mwh`, `total_scheduled_mwh`, `discrepancy_mwh` |
| `_eur` | Currency (euros) | `cycle_cost_eur`, `total_cost_eur`, `cost_eur` |
| `_eur_per_mw` | Price (EUR/MW) | `marginal_price_eur_per_mw`, `cleared_price_eur_per_mw` |
| `_eur_mwh` | Price (EUR/MWh) | `fill_price_eur_mwh`, `vwap_eur_mwh` |
| `_percent` | Percentage (0-100) | `soc_min_percent`, `initial_soc_percent`, `progress_percent` |
| `_seconds` | Duration (seconds) | `interval_seconds`, `clearing_delay_seconds` |

Unit conversions happen at explicit layer boundaries only, using functions from `temporal/helpers/units.py`: `kw_to_mw()`, `mw_to_kw()`, `kwh_to_mwh()`, `mwh_to_kwh()`, `kwh_to_average_mw()`.

---

## Dataclass Conventions

# TODO do we expect long-term alignment towards dataclasses or use pydantic for domain?

---

## Exception Organization

Custom exceptions are defined per-module, close to the code that raises them:

| Hierarchy | Base | Location |
|-----------|------|----------|
| Domain component errors | `ComponentError` (`src/core/component.py`) | Per-module (e.g., `PortfolioSettlementError`, `RunAuxiliaryDataStoreError`) |
| API service errors | `ServiceError` (`src/api/exceptions.py`) | Centralized in `src/api/exceptions.py` |
| Ledger errors | `LedgerStorageError` (`src/ledger/interfaces.py`) | Per-module within ledger |
| Orchestrator errors | `OrchestratorError`, `RunEngineError` | `src/orchestrator/` |

Naming: `{DomainNoun}Error`. NotFound variants: `{DomainNoun}NotFoundError`.

---

## Enum Conventions

| Type | When to Use | Examples |
|------|-------------|---------|
| `StrEnum` | Serialized values (API, storage, events) | `RunStatus`, `PnLAttributionMethod`, `Resolution` |
| `Enum` | Internal-only values not serialized as strings | `FlowDirection`, `CompressionLevel` |

---

## Module File Layout

Each domain module follows a consistent file structure:

Package `__init__.py` files define an explicit `__all__` list serving as the public API surface for the module.

---

## Section Separators

Within files, logical sections are delimited with box comments:

```python
# ============================================================================
# Section Name
# ============================================================================
```

Used in both source and test files to separate logical groups (e.g., interface implementation, private methods, test categories).
