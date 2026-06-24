Audit Report

## Title
Unbounded `disburse_maturity_in_progress` Queue Per SNS Neuron Causes Indefinitely Delayed Maturity Disbursements - (File: `rs/sns/governance/src/governance.rs`)

## Summary
SNS governance's `disburse_maturity` appends to `neuron.disburse_maturity_in_progress` with no upper-bound check, while the periodic finalizer `maybe_finalize_disburse_maturity` processes exactly one entry per neuron per invocation. Any principal holding `NeuronPermissionType::DisburseMaturity` can queue hundreds of disbursements on a single neuron, causing maturity to be deducted immediately from the neuron while the actual token transfers are serialized one-per-timer-tick, locking user funds in the queue for an arbitrarily long period. NNS governance prevents this with an explicit `MAX_NUM_DISBURSEMENTS = 10` guard that SNS governance does not replicate.

## Finding Description
`Governance::disburse_maturity` in `rs/sns/governance/src/governance.rs` performs percentage and minimum-amount validation, then unconditionally deducts `maturity_to_deduct` from `neuron.maturity_e8s_equivalent` and pushes a new `DisburseMaturityInProgress` entry with no check on the current queue length: [1](#0-0) 

The async finalizer `maybe_finalize_disburse_maturity` collects candidates by calling `.first()` on each neuron's queue — only the oldest entry is ever eligible per invocation: [2](#0-1) 

After a successful ledger transfer, the queue advances by exactly one via `remove(0)`: [3](#0-2) 

NNS governance defines and enforces a hard cap in `rs/nns/governance/src/governance/disburse_maturity.rs`: [4](#0-3) [5](#0-4) 

A `grep` across the entire SNS governance source confirms `MAX_NUM_DISBURSEMENTS` (or any equivalent constant) is absent from SNS governance entirely.

## Impact Explanation
A principal with `DisburseMaturity` permission — a standard, unprivileged neuron permission — can queue ~450 successive 1%-disbursements on a neuron holding ~1 ICP-equivalent of maturity. Each call immediately removes maturity from the neuron's balance; the tokens are not transferred until the finalizer processes that queue position. The N-th disbursement is not executed until N consecutive timer invocations after the 7-day delay window opens. With no protocol-enforced cap, the last disbursement in a 450-entry queue is delayed by hundreds of additional timer cycles beyond the intended 7-day window. User funds (maturity) are effectively locked in the queue with no recourse. This constitutes a **High** impact: significant SNS security impact with concrete, demonstrable user-funds harm — maturity is deducted immediately but disbursement is delayed for an unbounded period, directly harming SNS token holders.

## Likelihood Explanation
The attack requires only `NeuronPermissionType::DisburseMaturity` on a neuron with sufficient accumulated maturity. This is a standard neuron permission reachable by any SNS token holder via a normal `manage_neuron` ingress message. No governance majority, special key, or elevated privilege is needed. The minimum maturity to create a meaningful queue (tens of entries) is modest. The attack is repeatable and deterministic.

## Recommendation
Add a constant analogous to NNS governance's `MAX_NUM_DISBURSEMENTS` in SNS governance (a value of 7–10 is consistent with the 7-day disbursement delay and NNS precedent), and reject `disburse_maturity` calls when `neuron.disburse_maturity_in_progress.len() >= MAX_NUM_DISBURSEMENTS_SNS` before deducting any maturity. The check should be inserted in `disburse_maturity` after the percentage and minimum-amount validations and before the mutable re-borrow of the neuron.

## Proof of Concept
1. Acquire an SNS neuron with ≥ 1 ICP-equivalent of accumulated maturity.
2. Send ~450 successive `manage_neuron { DisburseMaturity { percentage_to_disburse: 1, … } }` ingress messages. Each call succeeds; each deducts maturity and appends to `disburse_maturity_in_progress`.
3. After 7 days, observe via canister state inspection that `maybe_finalize_disburse_maturity` processes only the first queue entry per timer tick; the remaining ~449 disbursements are processed one-per-tick.
4. Confirm that the identical sequence against NNS governance is rejected at the 11th call with `TooManyDisbursements` (enforced at `rs/nns/governance/src/governance/disburse_maturity.rs:306`), while SNS governance accepts all calls indefinitely. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1692-1698)
```rust
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L4954-4955)
```rust
                // The first entry is the oldest one, check whether it can be completed.
                let first_disbursement = neuron.disburse_maturity_in_progress.first()?;
```

**File:** rs/sns/governance/src/governance.rs (L5069-5069)
```rust
                    neuron.disburse_maturity_in_progress.remove(0);
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L40-40)
```rust
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L306-308)
```rust
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }
```
