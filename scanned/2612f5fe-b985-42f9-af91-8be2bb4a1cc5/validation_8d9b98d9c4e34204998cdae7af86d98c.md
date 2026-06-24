### Title
Stale Undocumented Magic Constant `0.05` in `spawn_neuron` Allows Spawning Neurons Below Minimum Stake After Mission 70 Modulation Range Expansion — (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The `spawn_neuron` function in NNS Governance uses an undocumented magic constant `0.05` (5%) to represent the worst-case maturity modulation when pre-validating whether a spawned neuron will meet the minimum stake requirement. Mission 70 expanded the negative bound of the maturity modulation from −5% to −10%, but the pre-check was never updated. Any neuron holder can now spawn a neuron whose final ICP stake, after the actual −10% modulation is applied, falls below `neuron_minimum_stake_e8s`, violating the minimum-stake invariant.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `spawn_neuron` function performs a pre-flight check to ensure the spawned neuron will have at least `neuron_minimum_stake_e8s` ICP after maturity modulation:

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
``` [1](#0-0) 

The literal `0.05` is undocumented — no comment explains why 5% is the worst case, nor does it reference the maturity modulation bounds constants. The value was historically correct when the CMC-backed maturity modulation range was `[−500, +500]` permyriad (i.e., −5% to +5%), defined in:

```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
``` [2](#0-1) 

However, Mission 70 introduced a new, wider modulation range with a lower bound of **−10%**:

```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
``` [3](#0-2) 

The CHANGELOG confirms that neuron spawning now reads the Mission 70 modulation value, not the old CMC-polled value:

> "Neuron spawning and maturity disbursement finalization now read the locally computed Mission 70 maturity modulation (derived from the XRC-backed price history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`." [4](#0-3) 

The actual modulation is applied in `maybe_spawn_neurons`:

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,
) {
``` [5](#0-4) 

Where `apply_maturity_modulation` computes:

```rust
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,
) -> Result<u64, String> {
    let amount_e8s = u128::from(amount_maturity_e8s);
    let adjusted_maturity_modulation_basis_points = saturating_add_or_subtract_u128_i32(
        BASIS_POINTS_PER_UNITY,
        maturity_modulation_basis_points,
    );
    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        ...
        .checked_div(BASIS_POINTS_PER_UNITY)
``` [6](#0-5) 

**Concrete example of invariant violation:**

Assume `neuron_minimum_stake_e8s = 100_000_000` (1 ICP):

| Step | Value |
|---|---|
| Minimum maturity to pass the 5% check | `100_000_000 / 0.95 ≈ 105_263_158` e8s |
| Actual stake after −10% Mission 70 modulation | `105_263_158 × 0.90 ≈ 94_736_842` e8s |
| Shortfall below minimum stake | `≈ 5_263_158` e8s (~0.05 ICP) |

The spawned neuron ends up with less than `neuron_minimum_stake_e8s`, violating the invariant the check was designed to enforce.

---

### Impact Explanation

Any NNS neuron holder can call `manage_neuron` with a `Spawn` command and supply a `maturity_to_spawn` value in the range `[neuron_minimum_stake_e8s / 0.95, neuron_minimum_stake_e8s / 0.90)`. The pre-check passes (because it uses 5%), but the actual spawned neuron receives less than `neuron_minimum_stake_e8s` ICP after the −10% Mission 70 modulation is applied. The result is:

1. **Minimum-stake invariant violated**: The spawned neuron has less ICP than the governance-enforced minimum, potentially preventing it from participating in governance (voting, proposing).
2. **User receives less ICP than the system guaranteed**: The error message explicitly promises the check prevents this outcome ("There isn't enough maturity to spawn a new neuron due to worst case maturity modulation"), but the guarantee is now false.
3. **Maturity is permanently consumed**: The parent neuron's maturity is deducted and the child neuron's maturity is set to zero before the ledger mint; there is no rollback path if the resulting stake is below minimum.

---

### Likelihood Explanation

- Mission 70 is **already enabled in production** (`ENABLE_MISSION_70_VOTING_REWARDS` defaults to `true`).
- The Mission 70 maturity modulation formula (`sensitivity * (recent_price − reference_price) / reference_price`) can reach −10% whenever the 7-day ICP price is sufficiently below the 365-day average.
- Any neuron holder can trigger this by calling `manage_neuron { Spawn { ... } }` — no privileged role required.
- The window of vulnerable maturity values is narrow (between the 5% and 10% worst-case thresholds), but it is deterministically reachable whenever modulation is near its minimum.

---

### Recommendation

1. Replace the undocumented literal `0.05` with a named constant that references the actual Mission 70 minimum modulation bound:

```rust
// Worst-case maturity modulation under Mission 70 is -10% = -1000 permyriad.
const WORST_CASE_MATURITY_MODULATION_FRACTION: f64 =
    (-MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as f64) / 10_000.0; // = 0.10

let least_possible_stake =
    (maturity_to_spawn as f64 * (1.0 - WORST_CASE_MATURITY_MODULATION_FRACTION)) as u64;
```

2. Add an inline comment citing the specific constant (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70`) so future changes to the modulation range automatically surface this dependency.

3. Add a unit test asserting that `least_possible_stake` computed with the worst-case modulation constant, when passed through `apply_maturity_modulation` at the minimum permyriad, always yields a value ≥ `neuron_minimum_stake_e8s`.

---

### Proof of Concept

**Entry path**: Unprivileged ingress call to `manage_neuron` on the NNS Governance canister.

**Steps**:

1. Hold a neuron with `maturity_e8s_equivalent = M` where:
   `neuron_minimum_stake_e8s / 0.95 ≤ M < neuron_minimum_stake_e8s / 0.90`

2. Call `manage_neuron { id: ..., command: Spawn { percentage_to_spawn: 100, ... } }`.

3. The pre-check at line 2666 computes `M * 0.95 ≥ neuron_minimum_stake_e8s` → **passes**.

4. The child neuron is created with `maturity_e8s_equivalent = M`.

5. When `maybe_spawn_neurons` fires (timer), it reads `maturity_modulation = −1000` permyriad (Mission 70 minimum) and calls `apply_maturity_modulation(M, −1000)`.

6. The resulting `neuron_stake = M * (10_000 − 1_000) / 10_000 = M * 0.90`.

7. Since `M * 0.90 < neuron_minimum_stake_e8s`, the spawned neuron's cached stake is below the minimum, violating the invariant the pre-check was supposed to enforce. [1](#0-0) [3](#0-2) [2](#0-1) [4](#0-3)

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

**File:** rs/nns/governance/src/governance.rs (L6484-6487)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L4-5)
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L11-26)
```rust
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,
) -> Result<u64, String> {
    let amount_e8s = u128::from(amount_maturity_e8s);

    let adjusted_maturity_modulation_basis_points = saturating_add_or_subtract_u128_i32(
        BASIS_POINTS_PER_UNITY,
        maturity_modulation_basis_points,
    );

    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?
        .checked_div(BASIS_POINTS_PER_UNITY)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/CHANGELOG.md (L20-22)
```markdown
* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```
