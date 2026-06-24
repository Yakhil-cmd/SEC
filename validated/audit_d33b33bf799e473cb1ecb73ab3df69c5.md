The code has been verified. Here is the audit result:

---

Audit Report

## Title
Missing Remaining-Maturity Guard Allows Permanent Sub-Minimum Dust After Partial Disbursement - (`File: rs/nns/governance/src/governance/disburse_maturity.rs`)

## Summary
`initiate_maturity_disbursement` validates that the *outgoing* disbursement amount meets `MINIMUM_DISBURSEMENT_E8S` (= `E8` = 100,000,000 e8s) but never checks that the *remaining* maturity after the operation is either zero or also at or above that minimum. A neuron controller can choose a percentage that passes the pre-check yet leaves a sub-minimum remainder that no subsequent `DisburseMaturity` call can ever disburse. For dissolved neurons, where `stake_maturity_of_neuron` is blocked, the stranded maturity is permanently irrecoverable.

## Finding Description
In `rs/nns/governance/src/governance/disburse_maturity.rs`, `initiate_maturity_disbursement` computes the disbursement amount and checks it against the minimum: [1](#0-0) 

After the check passes, the neuron's maturity is unconditionally reduced: [2](#0-1) 

There is no post-condition asserting that `maturity_e8s_equivalent - disbursement_maturity_e8s` is either zero or `>= MINIMUM_DISBURSEMENT_E8S`. The `MINIMUM_DISBURSEMENT_E8S` constant is confirmed at: [3](#0-2) 

**Concrete exploit path:**
1. Neuron has `maturity_e8s_equivalent = 150_000_000`.
2. Controller calls `DisburseMaturity { percentage_to_disburse: 67 }`.
3. `disbursement_maturity_e8s = 150_000_000 * 67 / 100 = 100_500_000 >= MINIMUM_DISBURSEMENT_E8S` → check passes.
4. Neuron maturity is set to `49_500_000`.
5. Any subsequent call with any percentage 1–100 yields `disbursement_maturity_e8s <= 49_500_000 < MINIMUM_DISBURSEMENT_E8S` → permanently rejected.

The only escape for non-dissolved neurons is `stake_maturity_of_neuron`, which has no minimum check. However, for dissolved neurons this path is blocked: [4](#0-3) 

The SNS `disburse_maturity` has the same structural gap — it checks the outgoing worst-case amount against `transaction_fee_e8s` but not the remaining balance: [5](#0-4) 

## Impact Explanation
Up to `MINIMUM_DISBURSEMENT_E8S − 1` = 99,999,999 e8s (~1 ICP at current values) of maturity per dissolved neuron can be permanently stranded with no recovery path. This constitutes a moderate, concrete, per-user permanent loss of in-scope ICP ledger assets, matching the **Medium ($200–$2,000)** bounty tier for moderate user-funds impact. No protocol-wide conservation break occurs, but individual user funds are permanently locked for dissolved neurons that fall into the affected maturity range.

## Likelihood Explanation
Any NNS neuron controller whose neuron is in `Dissolved` state with maturity in the range `(MINIMUM_DISBURSEMENT_E8S, 2 × MINIMUM_DISBURSEMENT_E8S)` — i.e., between 1 ICP and 2 ICP — can trigger this with a single `manage_neuron → DisburseMaturity` call using an appropriate percentage. No privileged role is required; the operation is fully permissionless. The condition is not exotic: neurons accumulate maturity continuously and dissolved neurons are common. The user need not be malicious — an honest user choosing a round percentage (e.g., 67%) can inadvertently strand their own funds.

## Recommendation
After computing `disbursement_maturity_e8s`, add a post-condition check before mutating the neuron:

```rust
let remaining = maturity_e8s_equivalent.saturating_sub(disbursement_maturity_e8s);
if remaining > 0 && remaining < MINIMUM_DISBURSEMENT_E8S {
    return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
        disbursement_maturity_e8s: remaining,
        minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
    });
}
```

Apply the analogous fix to SNS `disburse_maturity` in `rs/sns/governance/src/governance.rs`, checking the remaining maturity against `transaction_fee_e8s`.

## Proof of Concept
**Unit test plan** (safe, local, no mainnet interaction):

1. Create a `NeuronStore` with a dissolved neuron having `maturity_e8s_equivalent = 150_000_000`.
2. Call `initiate_maturity_disbursement` with `percentage_to_disburse = 67`.
3. Assert the call returns `Ok(100_500_000)` and the neuron's maturity is now `49_500_000`.
4. Call `initiate_maturity_disbursement` again with `percentage_to_disburse = 100`.
5. Assert the call returns `Err(DisbursementTooSmall { disbursement_maturity_e8s: 49_500_000, minimum_disbursement_e8s: 100_000_000 })`.
6. Confirm no `stake_maturity_of_neuron` path is available (dissolved state check fires).
7. Confirm the 49,500,000 e8s is permanently unrecoverable.

This test requires no external dependencies and can be run entirely within the existing `disburse_maturity_tests.rs` test module. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L45-45)
```rust
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L317-323)
```rust
    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
        })
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L708-710)
```rust
#[path = "disburse_maturity_tests.rs"]
#[cfg(test)]
mod tests;
```

**File:** rs/nns/governance/src/governance.rs (L2775-2780)
```rust
        if neuron_state == NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Neuron is dissolved.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1669-1678)
```rust
        if worst_case_maturity_modulation < transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "If worst case maturity modulation is applied (-5%) then this neuron would \
                     disburse {worst_case_maturity_modulation} e8s, but can't disburse an amount less than the transaction fee \
                     of {transaction_fee_e8s} e8s."
                ),
            ));
        }
```
