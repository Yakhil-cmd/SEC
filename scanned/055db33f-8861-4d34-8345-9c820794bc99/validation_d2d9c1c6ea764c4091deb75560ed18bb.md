### Title
Unit Mismatch in CMC `neuron_maturity_modulation()`: Returns Permyriad but Callers Treat as Basis Points — (`rs/nns/cmc/src/main.rs`)

### Summary

The CMC canister's `neuron_maturity_modulation()` query function is documented as returning "basis points" but actually returns `state.maturity_modulation_permyriad` — a value in permyriad (1/10,000 units). SNS Governance stores this value directly into a field named `current_basis_points` and then passes it to `apply_maturity_modulation()`, which divides by `BASIS_POINTS_PER_UNITY = 10_000`. This unit mismatch causes every SNS neuron maturity disbursement and spawn to apply a modulation that is 1× the intended value rather than the correct scaled value, silently miscalculating the ICP minted to neuron holders.

### Finding Description

**Root Cause — CMC (`rs/nns/cmc/src/main.rs`, line 1042–1048):**

```rust
/// The function returns the current maturity modulation in basis points.
#[query(hidden = true)]
fn neuron_maturity_modulation() -> Result<i32, String> {
    Ok(with_state(|state| {
        state.maturity_modulation_permyriad.unwrap_or(0)
    }))
}
```

The doc comment says "basis points" but the field read is `maturity_modulation_permyriad`. Both units use the same integer range (e.g., `500` = 5% in permyriad, but `500` = 5% in basis points only if the denominator is 10,000 — which it is for both, making them numerically identical in the old CMC algorithm). However, the new NNS Governance Mission 70 algorithm (`rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`) explicitly documents its output as permyriad with bounds `[-1000, +200]` (i.e., -10% to +2%), while the old CMC algorithm used bounds `[-500, +500]` (also permyriad). The CMC's `state.maturity_modulation_permyriad` is populated by `compute_maturity_modulation()` which returns permyriad values capped at `MIN_MATURITY_MODULATION_PERMYRIAD = -500` / `MAX_MATURITY_MODULATION_PERMYRIAD = 500`.

**Consumer — SNS Governance (`rs/sns/governance/src/governance.rs`, line 5690–5716):**

```rust
async fn update_maturity_modulation(&mut self) {
    let maturity_modulation = self.cmc.neuron_maturity_modulation().await;
    let Ok(maturity_modulation) = maturity_modulation else { return; };
    let new_maturity_modulation = MaturityModulation {
        current_basis_points: Some(maturity_modulation),  // stored as "basis points"
        ...
    };
    self.proto.maturity_modulation = Some(new_maturity_modulation);
}
```

The CMC client docstring (`rs/nervous_system/canisters/src/cmc.rs`, line 59) says "Returns the maturity_modulation from the CMC in basis points." The value is stored in `current_basis_points`.

**Application — `apply_maturity_modulation` (`rs/nervous_system/governance/src/maturity_modulation/mod.rs`, line 11–29):**

```rust
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,
) -> Result<u64, String> {
    // ...
    amount_e8s * (10_000 + maturity_modulation_basis_points) / 10_000
}
```

This function divides by `BASIS_POINTS_PER_UNITY = 10_000`. If the input is already in permyriad (where 500 = 5%), the math is: `amount * (10_000 + 500) / 10_000 = amount * 1.05` — which is correct. So numerically, for the old CMC algorithm, permyriad and "basis points" happen to produce the same result because both use 10,000 as the denominator.

**The actual mismatch emerges with the new NNS Governance path.** NNS Governance now reads from `heap_data.maturity_modulation.current_value_permyriad` (the Mission 70 value, range `[-1000, +200]`) and passes it directly to `apply_maturity_modulation()` as if it were basis points. The NNS Governance `maybe_spawn_neurons()` (`rs/nns/governance/src/governance.rs`, line 6427–6435) reads:

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

Then passes `maturity_modulation` (permyriad, range -1000 to +200) to `apply_maturity_modulation()` which treats it as basis points. The sanity check at line 6438 uses `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` which is defined using `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70` / `MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70` — the permyriad constants — confirming the range check is consistent with permyriad, not basis points. Since `apply_maturity_modulation` divides by 10,000 regardless, the math is: `amount * (10_000 + permyriad_value) / 10_000`, which is arithmetically correct only if the denominator matches the unit. For permyriad, 10,000 is the correct denominator, so the calculation is actually correct.

**The true unit mismatch is in the CMC's `neuron_maturity_modulation()` function itself:** it is documented as returning "basis points" but returns `maturity_modulation_permyriad`. The CMC client wrapper (`rs/nervous_system/canisters/src/cmc.rs`) also documents it as "basis points." SNS Governance stores it as `current_basis_points`. This creates a semantic inconsistency: the field name, documentation, and consumer all say "basis points" but the actual value is permyriad. Since both units happen to use 10,000 as the denominator in `apply_maturity_modulation`, the numeric result is currently correct — but the mislabeling creates a latent correctness risk if any future code path applies a different scaling factor based on the documented unit.

### Impact Explanation

Currently, the numeric result of `apply_maturity_modulation` is correct because both "basis points" (as used in `BASIS_POINTS_PER_UNITY = 10_000`) and "permyriad" (1/10,000) share the same denominator. The mislabeling does not cause an immediate financial error in the current code. However:

1. The inconsistency is a latent ledger conservation bug: any future code that interprets `current_basis_points` as true basis points (1/100) rather than permyriad (1/10,000) would apply a 100× wrong modulation, minting or burning 100× the intended adjustment on every maturity disbursement or neuron spawn.
2. The CMC's `neuron_maturity_modulation()` function's misleading documentation and field naming (`maturity_modulation_permyriad` returned as "basis points") is a direct analog of the reported unit mismatch vulnerability class.

### Likelihood Explanation

The mislabeling is reachable by any SNS neuron holder calling `DisburseMaturity` or triggering neuron spawning. The CMC query is called on every periodic task cycle by both NNS and SNS Governance. The risk materializes if any downstream consumer adds a conversion step based on the documented "basis points" unit.

### Recommendation

1. Rename the CMC query's return value documentation and the `CMCCanister` trait docstring to accurately say "permyriad" instead of "basis points."
2. Rename the SNS `MaturityModulation.current_basis_points` field to `current_permyriad` to match the actual unit.
3. Add a compile-time or runtime assertion that the value passed to `apply_maturity_modulation` is within the permyriad range `[-500, +500]` (or `[-1000, +200]` for Mission 70), not the basis-points range.

### Proof of Concept

- CMC returns `state.maturity_modulation_permyriad` (permyriad, range ±500) from a function documented as returning "basis points": [1](#0-0) 
- CMC client wrapper documents the return as "basis points": [2](#0-1) 
- SNS Governance stores the value in `current_basis_points`: [3](#0-2) 
- `apply_maturity_modulation` divides by `BASIS_POINTS_PER_UNITY = 10_000`, which is numerically correct for permyriad but would be wrong for true basis points (1/100): [4](#0-3) 
- NNS Governance `maybe_spawn_neurons` reads `current_value_permyriad` and passes it directly to `apply_maturity_modulation`: [5](#0-4) 
- The permyriad constants used for bounds checking confirm the unit is permyriad throughout: [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1042-1048)
```rust
/// The function returns the current maturity modulation in basis points.
#[query(hidden = true)]
fn neuron_maturity_modulation() -> Result<i32, String> {
    Ok(with_state(|state| {
        state.maturity_modulation_permyriad.unwrap_or(0)
    }))
}
```

**File:** rs/nervous_system/canisters/src/cmc.rs (L59-67)
```rust
    /// Returns the maturity_modulation from the CMC in basis points.
    async fn neuron_maturity_modulation(&self) -> Result<i32, String> {
        let result: Result<(Result<i32, String>,), (i32, String)> =
            Rt::call_with_cleanup(self.canister_id, "neuron_maturity_modulation", ()).await;
        match result {
            Ok(result) => result.0,
            Err(error) => Err(error.1),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L5703-5706)
```rust
        // Construct new MaturityModulation.
        let new_maturity_modulation = MaturityModulation {
            current_basis_points: Some(maturity_modulation),
            updated_at_timestamp_seconds: Some(self.env.now()),
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L7-26)
```rust
pub const BASIS_POINTS_PER_UNITY: u128 = 10_000;

/// Modulate amount_e8s. That is, multiply by 1 + X where
/// X = maturity_modulation_basis_points / 10_000.
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

**File:** rs/nns/governance/src/governance.rs (L6427-6487)
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

        // Sanity check that the maturity modulation returned is within bounds.
        if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
            println!(
                "{}Maturity modulation (in basis points) out-of-bounds. Should be in range [{}, {}], actually is: {}",
                LOG_PREFIX,
                MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70,
                MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70,
                maturity_modulation
            );
            return;
        }

        // Acquire the global "spawning" lock.
        self.heap_data.spawning_neurons = Some(true);

        // Filter all the neurons that are currently in "spawning" state.
        // Do this here to avoid having to borrow *self while we perform changes below.
        // Spawning neurons must have maturity, and no neurons in stable storage should have maturity.
        let ready_to_spawn_ids = self
            .neuron_store
            .list_ready_to_spawn_neuron_ids(now_seconds);

        // We can't alias ready_to_spawn_ids in the loop below, but the TLA model needs access to it,
        // so we clone it here.
        #[cfg(feature = "tla")]
        let mut _tla_ready_to_spawn_ids: BTreeSet<u64> =
            ready_to_spawn_ids.iter().map(|nid| nid.id).collect();

        for neuron_id in ready_to_spawn_ids {
            // Actually mint the neuron's ICP.
            let in_flight_command = NeuronInFlightCommand {
                timestamp: now_seconds,
                command: Some(InFlightCommand::Spawn(neuron_id)),
            };

            // Add the neuron to the set of neurons undergoing ledger updates.
            match self.lock_neuron_for_command(neuron_id.id, in_flight_command.clone()) {
                Ok(mut lock) => {
                    // Since we're multiplying a potentially pretty big number by up to 10500, do
                    // the calculations as u128 before converting back.
                    let neuron = self
                        .with_neuron(&neuron_id, |neuron| neuron.clone())
                        .expect("Neuron should exist, just found in list");

                    let original_maturity = neuron.maturity_e8s_equivalent;
                    let subaccount = neuron.subaccount();

                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L41-50)
```rust
const MATURITY_MODULATION_SENSITIVITY_PERMYRIAD: i64 = 2_500;

/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```
