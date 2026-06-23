### Title
Partial Maturity Disbursement Can Leave Sub-Minimum Dust Permanently Undisburse-able - (`File: rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary

`initiate_maturity_disbursement` in NNS governance enforces a minimum disbursement amount on the *outgoing* portion but never checks that the *remaining* maturity after the operation is either zero or also above `MINIMUM_DISBURSEMENT_E8S`. A caller can craft a percentage that satisfies the pre-check yet leaves a sub-minimum remainder that no subsequent call can ever disburse.

### Finding Description

`initiate_maturity_disbursement` computes the disbursement amount as `maturity * percentage / 100` and rejects it if it is below `MINIMUM_DISBURSEMENT_E8S` (= `E8` = 100 000 000 e8s): [1](#0-0) 

After the check passes, the neuron's maturity is reduced by exactly that amount: [2](#0-1) 

There is no guard that `maturity_e8s_equivalent - disbursement_maturity_e8s` is either zero or `>= MINIMUM_DISBURSEMENT_E8S`. The analogous Solidity bug (M-03) is identical in structure: only the *sent* amount is validated, not the *leftover*.

**Concrete scenario:**

| Step | Maturity | Action |
|------|----------|--------|
| Start | 150 000 000 e8s (1.5 ICP) | — |
| Disburse 67 % | disbursed = 100 500 000 ≥ E8 ✓ | succeeds |
| Remaining | **49 500 000 e8s** | < MINIMUM\_DISBURSEMENT\_E8S |
| Disburse 100 % of remaining | 49 500 000 < E8 | **rejected** |
| Disburse any % of remaining | always < E8 | **rejected** |

The 49 500 000 e8s of maturity is now permanently undisburse-able through `initiate_maturity_disbursement`.

### Impact Explanation

Up to `MINIMUM_DISBURSEMENT_E8S − 1` = 99 999 999 e8s (≈ 1 ICP at current values) of maturity per neuron can be permanently stranded. The only partial escape is `stake_maturity_of_neuron`, which has no minimum check and can absorb the dust into the neuron's stake — but that path is blocked when the neuron is already in `Dissolved` state: [3](#0-2) 

For dissolved neurons the maturity is irrecoverable. The SNS `disburse_maturity` has the same structural gap (remaining not checked against `transaction_fee_e8s`): [4](#0-3) 

**Impact:** Medium — small but real token loss per affected neuron; no protocol-wide conservation break, but individual user funds are permanently locked.

### Likelihood Explanation

Any NNS neuron controller whose maturity falls in the range `(MINIMUM_DISBURSEMENT_E8S, 2 × MINIMUM_DISBURSEMENT_E8S)` — i.e., between 1 ICP and 2 ICP — can trigger this with a single well-chosen percentage call. The operation is permissionless (any neuron controller can call `manage_neuron` → `DisburseMaturity`). No privileged role is required. [5](#0-4) 

### Recommendation

After computing `disbursement_maturity_e8s`, add a post-condition check:

```rust
let remaining = maturity_e8s_equivalent - disbursement_maturity_e8s;
if remaining > 0 && remaining < MINIMUM_DISBURSEMENT_E8S {
    return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
        disbursement_maturity_e8s: remaining,
        minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
    });
}
```

Apply the same fix to SNS `disburse_maturity` (check remaining against `transaction_fee_e8s`).

### Proof of Concept

Entry path (unprivileged ingress):

1. Caller controls an NNS neuron with `maturity_e8s_equivalent = 150_000_000`.
2. Caller sends `manage_neuron` → `DisburseMaturity { percentage_to_disburse: 67, to_account: None }`.
3. `initiate_maturity_disbursement` computes `disbursement_maturity_e8s = 150_000_000 * 67 / 100 = 100_500_000 ≥ MINIMUM_DISBURSEMENT_E8S` → check passes.
4. Neuron maturity is set to `49_500_000`.
5. Any subsequent `DisburseMaturity` call with any percentage 1–100 produces `disbursement_maturity_e8s ≤ 49_500_000 < MINIMUM_DISBURSEMENT_E8S` → permanently rejected. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L45-45)
```rust
pub const MINIMUM_DISBURSEMENT_E8S: u64 = E8;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L291-328)
```rust
    let disbursement_maturity_e8s =
        percentage_of_maturity(maturity_e8s_equivalent, *percentage_to_disburse)?;
    if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
        return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s,
            minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
        });
    }

    if is_neuron_spawning {
        return Err(InitiateMaturityDisbursementError::NeuronSpawning);
    }
    if !is_neuron_controlled_by_caller {
        return Err(InitiateMaturityDisbursementError::CallerIsNotNeuronController);
    }
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }

    let disbursement_in_progress = MaturityDisbursement {
        destination: Some(destination),
        amount_e8s: disbursement_maturity_e8s,
        timestamp_of_disbursement_seconds,
        finalize_disbursement_timestamp_seconds,
    };

    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
        })
        .map_err(|_| InitiateMaturityDisbursementError::Unknown {
            reason: "Failed to update neuron even though it was found before".to_string(),
        })?;

    Ok(disbursement_maturity_e8s)
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
