### Title
Stale 5% Worst-Case Modulation Bound in `spawn_neuron` Allows Sub-Minimum-Stake Neurons Under Mission 70's −10% Floor — (`rs/nns/governance/src/governance.rs`)

---

### Summary

`spawn_neuron` validates maturity sufficiency using a hardcoded 5% worst-case modulation factor. Mission 70 (Proposal 141441, April 2026) expanded the actual worst-case floor to −10% (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`). The validation check and the actual spawning path now use different bounds, so any unprivileged NNS neuron owner can spawn a child neuron whose maturity passes the pre-check but whose resulting ICP stake falls below `neuron_minimum_stake_e8s` when the live modulation is near −10%. The same stale-bound pattern exists in `initiate_maturity_disbursement`, where the comment explicitly states the check should account for worst-case modulation but the implementation does not apply any modulation factor at all.

---

### Finding Description

**Root cause 1 — `spawn_neuron` hardcoded 5% floor** [1](#0-0) 

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
```

The literal `0.05` encodes the old ±5% CMC-based modulation range. Mission 70 defines a new floor: [2](#0-1) 

```
MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000   // −10%
MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 =    200   // +2%
```

`maybe_spawn_neurons` reads the Mission 70 value at spawn time: [3](#0-2) 

and applies it via `apply_maturity_modulation`: [4](#0-3) 

So the validation gate uses 5% but the execution path uses up to 10%. Any maturity amount `M` satisfying:

```
M × 0.95 ≥ neuron_minimum_stake_e8s   (passes the gate)
M × 0.90 < neuron_minimum_stake_e8s   (fails at actual spawn)
```

i.e. `M ∈ [neuron_minimum_stake_e8s / 0.95, neuron_minimum_stake_e8s / 0.90)`, will produce a child neuron whose `cached_neuron_stake_e8s` is below the minimum.

**Root cause 2 — `initiate_maturity_disbursement` ignores modulation entirely**

The comment at the constant definition explicitly states the intent: [5](#0-4) 

> "A neuron can only disburse an amount of maturity that results in minting at least this many ICP (in e8) **assuming the worst case maturity modulation**."

But the actual check is: [6](#0-5) 

```rust
if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
```

No modulation factor is applied. A disbursement of exactly `MINIMUM_DISBURSEMENT_E8S` (1 ICP) at −10% modulation yields 0.9 ICP — below the stated minimum.

**The economic game (MEV analog)**

The maturity modulation is a public, price-sensitive parameter updated daily from ICP/XDR rates: [7](#0-6) 

Its trajectory is observable and bounded by a 30-permyriad/day speed limit: [8](#0-7) 

Because the modulation is applied at finalization (7 days after initiation), not at initiation: [9](#0-8) 

sophisticated users can observe the modulation trajectory and time their `Spawn` or `DisburseMaturity` calls to land in favorable windows — directly analogous to the DeFi report's "upperbound" timing game. The stale 5% bound widens this window by allowing spawns that would be rejected under a correct −10% check, creating neurons that exist with sub-minimum stake when the modulation is near its floor.

---

### Impact Explanation

1. **Sub-minimum-stake neurons**: A child neuron can be created with `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`. Such a neuron bypasses the minimum-stake invariant enforced everywhere else in governance. It may be ineligible to vote, earn rewards, or be merged, yet it consumes governance state and holds a ledger subaccount.

2. **Sub-minimum disbursements**: A maturity disbursement of exactly `MINIMUM_DISBURSEMENT_E8S` at −10% modulation mints 0.9 ICP — below the stated floor — violating the conservation guarantee the comment documents.

3. **Timing MEV**: Because modulation is public and speed-limited, users can predict when it will be near +2% and schedule spawns/disbursements to maximize ICP received, or delay spawns initiated near −10% to avoid the worst outcome — a first-mover advantage for users who monitor the modulation state.

---

### Likelihood Explanation

- Mission 70 is live on mainnet (Proposal 141441, April 2026). The −10% floor is reachable whenever ICP price drops significantly relative to its 365-day average.
- Any NNS neuron owner can call `manage_neuron { Spawn { ... } }` as an unprivileged ingress message. No special role or key is required.
- The maturity range that exploits the gap (`[neuron_minimum_stake_e8s/0.95, neuron_minimum_stake_e8s/0.90)`) is narrow but reachable by any neuron with accumulated maturity near the minimum stake threshold.
- The modulation value and its daily trajectory are publicly queryable, making timing straightforward.

---

### Recommendation

1. **Fix `spawn_neuron`**: Replace the hardcoded `0.05` with the actual Mission 70 worst-case floor:
   ```rust
   // Use MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000 (−10%)
   let least_possible_stake = apply_maturity_modulation(
       maturity_to_spawn,
       MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
   ).unwrap_or(0);
   ```

2. **Fix `initiate_maturity_disbursement`**: Apply the worst-case modulation before comparing to `MINIMUM_DISBURSEMENT_E8S`, consistent with the comment's stated intent:
   ```rust
   let worst_case_amount = apply_maturity_modulation(
       disbursement_maturity_e8s,
       MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
   ).unwrap_or(0);
   if worst_case_amount < MINIMUM_DISBURSEMENT_E8S { ... }
   ```

3. **Introduce a named constant** for the worst-case bound used in validation so it stays in sync with `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70` as the range evolves.

---

### Proof of Concept

Assume `neuron_minimum_stake_e8s = 100_000_000` (1 ICP, the default).

**Step 1**: Neuron owner accumulates maturity of `106_000_000` e8s (~1.06 ICP).

**Step 2**: Owner calls `manage_neuron { id, Spawn { percentage_to_spawn: 100, ... } }`.

**Step 3**: Validation in `spawn_neuron`:
```
least_possible_stake = 106_000_000 × 0.95 = 100_700_000 ≥ 100_000_000  ✓ passes
```

**Step 4**: Child neuron is created in spawning state with `maturity_e8s_equivalent = 106_000_000`.

**Step 5**: 7 days later, `maybe_spawn_neurons` fires. Suppose `maturity_modulation.current_value_permyriad = -1_000` (−10%, the Mission 70 floor).

**Step 6**: `apply_maturity_modulation(106_000_000, -1_000)`:
```
106_000_000 × (10_000 − 1_000) / 10_000 = 106_000_000 × 0.90 = 95_400_000
```

**Step 7**: `cached_neuron_stake_e8s = 95_400_000 < 100_000_000 = neuron_minimum_stake_e8s`.

The child neuron now exists on the ledger with a sub-minimum stake, bypassing the invariant that `spawn_neuron` was designed to enforce. [1](#0-0) [4](#0-3) [2](#0-1) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2664-2673)
```rust
        // Check if the least possible stake this neuron would be spawned with
        // is more than the minimum neuron stake.
        let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

        if least_possible_stake < economics.neuron_minimum_stake_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L6427-6435)
```rust
        let maturity_modulation = match self
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad)
        {
            None => return,
            Some(value) => value,
        };
```

**File:** rs/nns/governance/src/governance.rs (L6484-6487)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-44)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L116-158)
```rust
/// Compute the new maturity modulation in permyriad.
///
/// Compares the current ICP price (7-day moving average) to the reference ICP price (365-day
/// moving average) and computes:
///
///   `target = sensitivity * (current_price - reference_price) / reference_price`
///
/// On the first calculation (`previous` is `None`), the target is returned subject only to global
/// bounds — the speed limit needs a baseline to be meaningful. On subsequent calculations a daily
/// speed limit smooths day-to-day change, and global bounds have final say.
///
/// Returns `Err` with a reason if the inputs make the calculation impossible (e.g. price history
/// is incomplete or the reference price is zero). Callers that hit `Err` should leave the prior
/// modulation value untouched and log the reason.
fn compute_maturity_modulation_permyriad(
    rates: &[SampledPrice],
    current_day: u64,
    previous: Option<(i64, u64)>,
) -> Result<i64, String> {
    let recent_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the recent price window".to_string())?;

    let reference_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the reference price window".to_string())?;

    if reference_icp_price == 0 {
        return Err("reference price averaged to zero".to_string());
    }

    let target_modulation = {
        let recent = recent_icp_price as i128;
        let reference = reference_icp_price as i128;
        let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
        sensitivity * (recent - reference) / reference
    };
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L36-37)
```rust
/// The delay in seconds between initiating a maturity disbursement and the actual disbursement.
const DISBURSEMENT_DELAY_SECONDS: u64 = ONE_DAY_SECONDS * 7;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L41-45)
```rust
/// The minimum amount of ICP that need to be minted when disbursing maturity. A neuron can only
/// disburse an amount of maturity that results in minting at least this many ICP (in e8) assuming
/// the worst case maturity modulation. This limit is set to be consistent with the neuron spawning
/// behavior (which maturity disbursement is designed to replace).
pub const MINIMUM_DISBURSEMENT_E8S: u64 = E8;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L291-298)
```rust
    let disbursement_maturity_e8s =
        percentage_of_maturity(maturity_e8s_equivalent, *percentage_to_disburse)?;
    if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
        return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s,
            minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
        });
    }
```
