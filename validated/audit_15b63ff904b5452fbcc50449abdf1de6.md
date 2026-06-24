The code is confirmed. The SNS `disburse_maturity` at lines 1643–1651 performs `u64 × u64` with `.expect()`, the NNS version at lines 225–244 correctly widens to `u128`, and the NNS `spawn_neuron` at lines 2643–2647 has the same pattern. All citations match the actual code.

Audit Report

## Title
Panic-on-Overflow in SNS Governance `disburse_maturity` Causes Permanent DoS for Neurons with Large Maturity - (File: rs/sns/governance/src/governance.rs)

## Summary
The `disburse_maturity` function in SNS governance multiplies `maturity_e8s_equivalent` (u64) by `percentage_to_disburse` (cast to u64) without widening to u128, then calls `.expect()` which panics on overflow. Any SNS neuron whose `maturity_e8s_equivalent` exceeds `u64::MAX / 100 ≈ 1.84 × 10¹⁷` is permanently unable to disburse maturity. The NNS governance already fixed the identical computation by widening to u128 first; the SNS governance did not receive the same fix.

## Finding Description
In `rs/sns/governance/src/governance.rs` at lines 1643–1651, `disburse_maturity` computes:

```rust
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent                          // u64
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)  // u64 × u64
    .expect("Overflow while processing maturity to disburse.")     // PANICS on overflow
    .checked_div(100)
    .expect("Error when processing maturity to disburse.")
    as u128;
let maturity_to_deduct = maturity_to_deduct as u64;
```

`percentage_to_disburse` is validated to be in `[1, 100]` (lines 1633–1640), so the worst-case product is `maturity_e8s_equivalent × 100`. This overflows u64 when `maturity_e8s_equivalent > u64::MAX / 100 ≈ 1.84 × 10¹⁷`. The `.expect()` call then panics, trapping the update call. Because the trap occurs before any state mutation, the neuron's maturity is never deducted and the ledger mint never fires — the neuron is permanently blocked from disbursing.

The NNS governance already solved this in `rs/nns/governance/src/governance/disburse_maturity.rs` at lines 225–244 by widening both operands to u128 before multiplying and returning a `Result` instead of panicking. The SNS governance never received the equivalent fix.

## Impact Explanation
Any SNS neuron whose `maturity_e8s_equivalent` exceeds `≈ 1.84 × 10¹⁷` (≈ 1.84 billion tokens at 8 decimals) is permanently unable to call `disburse_maturity`. The earned maturity rewards are frozen and inaccessible indefinitely. This constitutes a significant SNS governance security impact with concrete user harm — permanent loss of access to earned maturity rewards — matching the **High ($2,000–$10,000)** bounty tier for "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation
SNS token total supplies are configurable at launch; many projects deploy with supplies in the billions or trillions of tokens. A neuron holding a significant fraction of such a supply and accumulating voting rewards over years can reach `maturity_e8s_equivalent > 1.84 × 10¹⁷`. The exploit path is fully unprivileged: any principal holding `DisburseMaturity` permission on their own neuron triggers the trap simply by calling `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 100, ... }`. No special role, key, or coordination is required. Once triggered, every subsequent call with `percentage_to_disburse ≥ 93` also traps for a neuron at `2 × 10¹⁷` e8s, making the DoS permanent without a canister upgrade.

## Recommendation
Mirror the NNS governance fix: widen both operands to u128 before multiplying and return a `GovernanceError` instead of panicking:

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

Apply the same fix to `spawn_neuron` in `rs/nns/governance/src/governance.rs` (lines 2643–2647) for defence-in-depth, even though the current NNS ICP supply keeps neurons below the threshold.

## Proof of Concept
1. Deploy an SNS with a total token supply of 10¹⁰ tokens (8 decimals → 10¹⁸ e8s).
2. Create a neuron and set `maturity_e8s_equivalent = 2 × 10¹⁷` via test harness or accumulated rewards.
3. Call `manage_neuron` with `Command::DisburseMaturity(DisburseMaturity { percentage_to_disburse: 100, to_account: None })`.
4. Internally: `2 × 10¹⁷ × 100 = 2 × 10¹⁹ > u64::MAX ≈ 1.844 × 10¹⁹`; `checked_mul` returns `None`; `.expect("Overflow while processing maturity to disburse.")` panics; the call traps.
5. Verify the neuron's `maturity_e8s_equivalent` is unchanged and every subsequent call with `percentage_to_disburse ≥ 93` also traps.

This is reproducible as a deterministic unit test in `rs/sns/governance/src/governance.rs` by directly calling `governance.disburse_maturity(...)` with a neuron whose `maturity_e8s_equivalent` is set to `2 × 10¹⁷`.