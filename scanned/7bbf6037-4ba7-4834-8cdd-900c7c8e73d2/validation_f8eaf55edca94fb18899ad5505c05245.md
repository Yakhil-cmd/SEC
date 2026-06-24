### Title
Unchecked `u64` Multiplication Overflow in `merge_maturity` Causes Silent Maturity Destruction - (`rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `merge_maturity` function computes the amount of maturity to merge using a plain `u64` multiplication without overflow protection. For SNS tokens with large supplies, a neuron controller can trigger silent integer overflow, causing the neuron's maturity to be reduced by a tiny wrapped value while the stake is increased by the same tiny value. The user permanently loses the bulk of their maturity without receiving the corresponding stake — a ledger conservation violation. The NNS governance equivalent correctly uses `u128` arithmetic to prevent this.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `merge_maturity` function computes the amount to merge as:

```rust
let mut maturity_to_merge =
    (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;
``` [1](#0-0) 

Both operands are `u64`. If `maturity_e8s_equivalent > u64::MAX / percentage_to_merge`, the multiplication silently wraps in release mode (Rust's default), producing a value far smaller than the correct result. The subsequent guard:

```rust
if maturity_to_merge > neuron.maturity_e8s_equivalent {
    maturity_to_merge = neuron.maturity_e8s_equivalent;
}
``` [2](#0-1) 

only catches the case where overflow produces a value *larger* than `maturity_e8s_equivalent`. When overflow wraps to a *smaller* value (the typical case), the guard is bypassed entirely.

The NNS governance equivalent, `percentage_of_maturity`, correctly widens to `u128` before multiplying:

```rust
(total_maturity_e8s as u128)
    .checked_mul(percentage_to_disburse as u128)
    .and_then(|result| result.checked_div(100))
``` [3](#0-2) 

This is the same class of bug as the Livepeer finding: one code path uses the correct precision (`u128` / `PreciseMathUtils`) while another uses an insufficient precision (`u64` / `MathUtils`), causing the intermediate product to overflow and produce a drastically wrong result.

### Impact Explanation

After overflow, both the maturity deduction and the stake credit use the same small wrapped value:

```rust
neuron.maturity_e8s_equivalent = neuron
    .maturity_e8s_equivalent
    .saturating_sub(maturity_to_merge);
let new_stake = neuron
    .cached_neuron_stake_e8s
    .saturating_add(maturity_to_merge);
``` [4](#0-3) 

The neuron loses only the tiny wrapped amount of maturity, but also only gains that tiny amount of stake. The difference — the bulk of the intended merge — is silently destroyed. Tokens are neither in maturity nor in stake: a permanent ledger conservation violation for the affected neuron.

### Likelihood Explanation

The overflow threshold is `u64::MAX / percentage_to_merge`. For `percentage_to_merge = 100`, this is `≈ 1.84 × 10^17 e8s` (≈ 1.84 billion tokens with 8 decimals). Many SNS deployments have total supplies in the billions of tokens. A neuron that has accumulated maturity above this threshold — possible for a large, long-staked neuron in a high-supply SNS — will silently trigger the overflow when calling `merge_maturity` with a high percentage. The call is unprivileged: any holder of `NeuronPermissionType::MergeMaturity` can invoke it.

### Recommendation

Replace the unchecked `u64` multiplication with a `u128`-widened calculation, matching the NNS pattern:

```rust
let maturity_to_merge = u64::try_from(
    (neuron.maturity_e8s_equivalent as u128)
        .checked_mul(merge_maturity.percentage_to_merge as u128)
        .expect("overflow computing maturity_to_merge")
        / 100_u128,
).expect("maturity_to_merge exceeds u64::MAX");
```

This mirrors the safe pattern already used in `percentage_of_maturity` in NNS governance. [5](#0-4) 

### Proof of Concept

Let `maturity_e8s_equivalent = 2_000_000_000_00000000` (2 billion tokens, 8 decimals = `2 × 10^17 e8s`) and `percentage_to_merge = 100`.

1. `2e17 * 100 = 2e19`. `u64::MAX ≈ 1.844e19`. Overflow wraps: `2e19 - 2^64 ≈ 1.553e18`.
2. `maturity_to_merge = 1.553e18 / 100 = 1.553e16`.
3. Guard check: `1.553e16 > 2e17`? **No** — guard is bypassed.
4. `maturity_e8s_equivalent` is reduced by `1.553e16` (not `2e17`).
5. `cached_neuron_stake_e8s` is increased by `1.553e16` (not `2e17`).
6. Net loss: `≈ 1.845e17 e8s` of maturity is permanently destroyed.

The attacker-controlled entry path is a direct ingress call to the SNS governance canister's `manage_neuron` endpoint with a `MergeMaturity` command, requiring only `NeuronPermissionType::MergeMaturity` on the caller's own neuron. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1453-1481)
```rust
    pub async fn merge_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        merge_maturity: &manage_neuron::MergeMaturity,
    ) -> Result<MergeMaturityResponse, GovernanceError> {
        let now = self.env.now();

        let neuron = self.get_neuron_result(id)?.clone();

        neuron.check_authorized(caller, NeuronPermissionType::MergeMaturity)?;

        if merge_maturity.percentage_to_merge > 100 || merge_maturity.percentage_to_merge == 0 {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to merge must be a value between 1 and 100 (inclusive).",
            ));
        }

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        let mut maturity_to_merge =
            (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;

        // Converting u64 to f64 can cause the u64 to be "rounded up", so we
        // need to account for this possibility.
        if maturity_to_merge > neuron.maturity_e8s_equivalent {
            maturity_to_merge = neuron.maturity_e8s_equivalent;
        }
```

**File:** rs/sns/governance/src/governance.rs (L1515-1521)
```rust
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_merge);
        let new_stake = neuron
            .cached_neuron_stake_e8s
            .saturating_add(maturity_to_merge);
        neuron.update_stake(new_stake, now);
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
