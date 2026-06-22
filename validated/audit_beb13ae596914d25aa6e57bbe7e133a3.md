### Title
Panic-on-Overflow in SNS Governance `disburse_maturity` Causes Permanent DoS for Neurons with Large Maturity - (File: rs/sns/governance/src/governance.rs)

### Summary
The `disburse_maturity` function in SNS governance performs a `u64 × u64` multiplication using `checked_mul(...).expect(...)`. When `maturity_e8s_equivalent × percentage_to_disburse` overflows `u64`, the canister call traps. Any neuron whose maturity exceeds `u64::MAX / 100 ≈ 1.84 × 10¹⁷ e8s` is permanently unable to disburse maturity. The NNS governance already fixed the identical computation by widening to `u128` first; the SNS governance did not receive the same fix.

### Finding Description
In `rs/sns/governance/src/governance.rs`, `disburse_maturity` computes the amount to deduct entirely in `u64`:

```rust
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent                          // u64
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)  // u64 × u64
    .expect("Overflow while processing maturity to disburse.")     // PANIC on overflow
    .checked_div(100)
    .expect("Error when processing maturity to disburse.")
    as u128;

let maturity_to_deduct = maturity_to_deduct as u64;
```

`percentage_to_disburse` is validated to be in `[1, 100]`, so the worst-case product is `maturity_e8s_equivalent × 100`. This overflows `u64` whenever `maturity_e8s_equivalent > u64::MAX / 100 ≈ 1.84 × 10¹⁷`. The `.expect()` call then panics, trapping the update call and permanently blocking the neuron from disbursing.

The NNS governance already solved this identically-structured problem by widening to `u128` before multiplying:

```rust
// rs/nns/governance/src/governance/disburse_maturity.rs
fn percentage_of_maturity(total_maturity_e8s: u64, percentage_to_disburse: u32)
    -> Result<u64, InitiateMaturityDisbursementError>
{
    (total_maturity_e8s as u128)
        .checked_mul(percentage_to_disburse as u128)
        .and_then(|result| result.checked_div(100))
        ...
        .ok_or_else(|| InitiateMaturityDisbursementError::Unknown { ... })
}
```

The SNS governance never received this fix. [1](#0-0) [2](#0-1) 

### Impact Explanation
Any SNS neuron whose `maturity_e8s_equivalent` exceeds `u64::MAX / 100 ≈ 1.84 × 10¹⁷` (≈ 1.84 billion tokens with 8 decimals) is permanently unable to call `disburse_maturity`. Every call traps before any state mutation occurs, so the maturity is never deducted and the ledger mint never fires. The neuron holder loses access to their earned maturity rewards indefinitely. Because the trap happens before any state change, the canister itself remains live, but the affected neuron's maturity is frozen.

The same `u64` overflow pattern exists in NNS `spawn_neuron`:

```rust
let maturity_to_spawn = parent_neuron
    .maturity_e8s_equivalent
    .checked_mul(percentage as u64)
    .expect("Overflow while processing maturity to spawn.");
``` [3](#0-2) 

For NNS the total ICP supply (~5 × 10¹⁶ e8s) keeps all neurons below the threshold, but for SNS tokens with large total supplies the threshold is reachable.

### Likelihood Explanation
SNS token total supplies are configurable at launch and many projects deploy with supplies in the billions or trillions of tokens. A neuron holding a significant fraction of such a supply and accumulating voting rewards over years can reach `maturity_e8s_equivalent > 1.84 × 10¹⁷`. The entry path is fully unprivileged: any principal with `DisburseMaturity` permission on their own neuron can trigger the trap simply by calling `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 100, ... }`. No special role or key is required. [4](#0-3) [5](#0-4) 

### Recommendation
Mirror the NNS governance fix: widen both operands to `u128` before multiplying and return a `GovernanceError` instead of panicking:

```rust
let maturity_to_deduct: u64 = (neuron.maturity_e8s_equivalent as u128)
    .checked_mul(disburse_maturity.percentage_to_disburse as u128)
    .and_then(|v| v.checked_div(100))
    .and_then(|v| u64::try_from(v).ok())
    .ok_or_else(|| GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Overflow computing maturity to disburse",
    ))?;
```

Apply the same fix to `spawn_neuron` in `rs/nns/governance/src/governance.rs` for defence-in-depth, even though the NNS supply currently keeps it safe.

### Proof of Concept
1. Deploy an SNS with a total token supply of, e.g., 10¹⁰ tokens (8 decimals → 10¹⁸ e8s).
2. Create a neuron and, over time (or via test harness), set `maturity_e8s_equivalent = 2 × 10¹⁷` (achievable if the neuron holds a large fraction of supply).
3. Call `manage_neuron` with `Command::DisburseMaturity(DisburseMaturity { percentage_to_disburse: 100, to_account: None })`.
4. Internally, `2 × 10¹⁷ × 100 = 2 × 10¹⁹ > u64::MAX ≈ 1.84 × 10¹⁹`; `checked_mul` returns `None`; `.expect(...)` panics; the call traps.
5. The neuron's maturity is unchanged, but every subsequent `disburse_maturity` call with `percentage_to_disburse ≥ 93` will also trap (`2 × 10¹⁷ × 93 ≈ 1.86 × 10¹⁹ > u64::MAX`), permanently blocking maturity disbursement for this neuron. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1616)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
```

**File:** rs/sns/governance/src/governance.rs (L1633-1640)
```rust
        if disburse_maturity.percentage_to_disburse > 100
            || disburse_maturity.percentage_to_disburse == 0
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to disburse must be a value between 1 and 100 (inclusive).",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1643-1651)
```rust
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;

        let maturity_to_deduct = maturity_to_deduct as u64;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L225-244)
```rust
fn percentage_of_maturity(
    total_maturity_e8s: u64,
    percentage_to_disburse: u32,
) -> Result<u64, InitiateMaturityDisbursementError> {
    (total_maturity_e8s as u128)
        .checked_mul(percentage_to_disburse as u128)
        .and_then(|result| result.checked_div(100))
        .and_then(|result| {
            // This should be impossible as long as `percentage_to_disburse` is between 0 and 100.
            if result > u64::MAX as u128 {
                None
            } else {
                Some(result as u64)
            }
        })
        .ok_or_else(|| InitiateMaturityDisbursementError::Unknown {
            reason: format!(
                "Failed to calculate percentage of maturity: {percentage_to_disburse}% of {total_maturity_e8s} e8s"
            ),
        })
```

**File:** rs/nns/governance/src/governance.rs (L2643-2647)
```rust
        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
        let maturity_to_spawn = maturity_to_spawn.checked_div(100).unwrap();
```
