### Title
`u64` Multiplication Overflow in `disburse_maturity` Causes Governance Canister Trap - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

`Governance::disburse_maturity` in the SNS governance canister performs a `u64 × u64` multiplication without widening to `u128`, then calls `.expect()` on the `checked_mul` result. When a neuron's `maturity_e8s_equivalent` is large enough, the multiplication overflows `u64`, `checked_mul` returns `None`, and `.expect()` panics, trapping the canister update call. The neuron controller is permanently unable to disburse their maturity. The NNS governance already fixed the identical bug in its own `percentage_of_maturity` helper by widening to `u128` first — but the SNS governance was never updated to match.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `Governance::disburse_maturity` computes the amount to deduct as:

```rust
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent                                        // u64
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)  // u64 × u64
    .expect("Overflow while processing maturity to disburse.")      // panics on None
    .checked_div(100)
    .expect("Error when processing maturity to disburse.")
    as u128;
``` [1](#0-0) 

Both operands are `u64`. `checked_mul` returns `None` when the product exceeds `u64::MAX ≈ 1.844 × 10¹⁹`. The `.expect()` call then panics, which in the IC execution model causes the update call to **trap** and roll back all state changes.

The overflow threshold for the worst case (`percentage_to_disburse = 100`) is:

```
u64::MAX / 100 ≈ 1.844 × 10¹⁷ e8s ≈ 1.844 billion tokens
```

For smaller percentages the threshold is proportionally higher (e.g., `percentage_to_disburse = 2` overflows above ~9.22 × 10¹⁸ e8s).

The NNS governance already fixed the identical pattern in its own `percentage_of_maturity` helper by widening both operands to `u128` before multiplying:

```rust
fn percentage_of_maturity(total_maturity_e8s: u64, percentage_to_disburse: u32) -> Result<u64, ...> {
    (total_maturity_e8s as u128)
        .checked_mul(percentage_to_disburse as u128)
        ...
}
``` [2](#0-1) 

The SNS governance `disburse_maturity` was never updated to apply the same widening.

`maturity_e8s_equivalent` is a `u64` field on the SNS neuron proto, and `percentage_to_disburse` is a `u32` (range 1–100) on the `DisburseMaturity` message. [3](#0-2) 

---

### Impact Explanation

A neuron controller whose neuron's `maturity_e8s_equivalent` meets or exceeds `u64::MAX / percentage_to_disburse` cannot disburse any portion of their maturity. Every call to `manage_neuron { DisburseMaturity { percentage_to_disburse: P, ... } }` traps the SNS governance canister update call. Because the maturity is not deducted (the trap rolls back state), the maturity remains locked in the neuron but is permanently inaccessible via this path. The neuron controller loses the ability to convert accumulated maturity into liquid SNS tokens.

**Vulnerability class:** cycles/resource accounting bug (bounded-integer arithmetic overflow causing canister trap that blocks a governance financial operation).

---

### Likelihood Explanation

SNS tokens can have very large total supplies (billions of tokens). A neuron that holds a large stake and has been staking for a long time can accumulate maturity in the billions-of-tokens range. The threshold of ~1.844 billion tokens of maturity (at 100% disbursement) is reachable for large SNS DAOs. The entry path is a standard unprivileged `manage_neuron` ingress call — no special role or key is required beyond controlling the neuron.

---

### Recommendation

Widen `maturity_e8s_equivalent` to `u128` before multiplying, matching the NNS governance fix:

```rust
// Before (overflows for large maturity):
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)
    .expect("Overflow while processing maturity to disburse.")
    .checked_div(100)
    .expect("Error when processing maturity to disburse.")
    as u128;

// After (safe):
let maturity_to_deduct = (neuron.maturity_e8s_equivalent as u128)
    .checked_mul(disburse_maturity.percentage_to_disburse as u128)
    .and_then(|v| v.checked_div(100))
    .expect("Overflow while processing maturity to disburse.")
    as u64;
```

This mirrors the pattern already used in `rs/nns/governance/src/governance/disburse_maturity.rs`. [4](#0-3) 

---

### Proof of Concept

1. Deploy an SNS with a token that has a large total supply.
2. Create a neuron and stake a large amount (or accumulate maturity over time until `maturity_e8s_equivalent ≥ u64::MAX / 100 ≈ 1.844 × 10¹⁷`).
3. Call `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 100, to_account: None }` as the neuron controller.
4. The SNS governance canister traps with the message `"Overflow while processing maturity to disburse."` — the call returns an error and no maturity is disbursed.
5. Repeat with any `percentage_to_disburse` value; the call always traps as long as `maturity_e8s_equivalent * percentage_to_disburse > u64::MAX`.

The vulnerable line is: [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1614)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
```

**File:** rs/sns/governance/src/governance.rs (L1643-1649)
```rust
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;
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
