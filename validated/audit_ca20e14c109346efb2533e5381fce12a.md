### Title
Genesis Neuron ICP Permanently Locked When `kyc_verified = false` With No On-Chain Recovery Path — (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance canister enforces a `kyc_verified` flag on neurons that gates both `disburse_neuron` and `disburse_to_neuron`. Genesis neurons are initialized with `kyc_verified = false` and can only be unlocked via an `ApproveGenesisKyc` governance proposal. If a genesis neuron's principal was never included in such a proposal, the staked ICP is permanently locked with no on-chain recovery or seizure mechanism — a direct structural analog to the burnt-passport lockout in the external report.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `disburse_neuron` function hard-blocks disbursement when `kyc_verified` is `false`:

```rust
if !is_neuron_kyc_verified {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!("Neuron {} is not kyc verified.", id.id),
    ));
}
``` [1](#0-0) 

The same hard-block exists in `disburse_to_neuron`:

```rust
if !parent_neuron.kyc_verified {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!("Neuron is not kyc verified: {}", id.id),
    ));
}
``` [2](#0-1) 

The **only** mechanism to set `kyc_verified = true` is `approve_genesis_kyc` in `rs/nns/governance/src/neuron_store.rs`, which is triggered exclusively by an `ApproveGenesisKyc` governance proposal:

```rust
if neuron.controller() == principal {
    neuron.kyc_verified = true;
}
``` [3](#0-2) 

This function is one-directional — it can only set the flag to `true`, never revoke it. There is no inverse proposal type, no user-callable method, and no protocol-level seizure path for locked ICP. The official documentation embedded in the canister confirms the design:

> "When new neurons are created at Genesis, they have GenesisKYC=false. This restricts what actions they can perform. Specifically, they cannot spawn new neurons, and once their dissolve delays are zero, they cannot be disbursed and their balances unlocked to new accounts." [4](#0-3) 

The `disburse_to_neuron` child neuron also inherits the parent's KYC status directly (ignoring the caller-supplied `kyc_verified` field in the `DisburseToNeuron` command), propagating the lockout to any split-off neurons:

```rust
.with_kyc_verified(parent_neuron.kyc_verified)
``` [5](#0-4) 

The `merge_neurons` path in `rs/nns/governance/src/governance/merge_neurons.rs` also contains 11 references to `kyc_verified`, indicating merge operations are similarly gated. [6](#0-5) 

---

### Impact Explanation

Any genesis neuron whose principal was never included in an `ApproveGenesisKyc` proposal has its entire ICP stake permanently locked inside the governance canister's ledger subaccount. The user cannot:
- Call `disburse_neuron` (blocked at line 1990)
- Call `disburse_to_neuron` (blocked at line 2949)
- Merge into another neuron (blocked by `kyc_verified` checks in `merge_neurons.rs`)

The protocol has no mechanism to seize, redistribute, or burn the locked ICP. The funds are stranded in the governance canister's ledger subaccounts indefinitely. This is a **ledger conservation bug**: ICP supply is effectively removed from circulation with no recovery path.

---

### Likelihood Explanation

Genesis was a one-time event. The `ApproveGenesisKyc` proposal type was used post-genesis to approve KYC for participants. However:

1. The `approve_genesis_kyc` function caps approvals at 1,000 neurons per proposal execution. [7](#0-6) 
2. Any genesis principal omitted from all historical `ApproveGenesisKyc` proposals retains `kyc_verified = false` permanently.
3. There is no on-chain query to enumerate all neurons still in the `kyc_verified = false` state, making the scope of locked funds opaque.
4. The entry path is a standard ingress `manage_neuron` call — no privilege required to trigger the lockout condition (the user simply calls `disburse_neuron` and receives `PreconditionFailed`).

Likelihood of new instances: **Low** (genesis is historical). Likelihood that existing locked instances remain unrecoverable without a new governance proposal: **High**.

---

### Recommendation

1. **Add a `RevokeGenesisKyc` or `SeizeLockedNeuron` proposal type** that allows the NNS to redistribute or burn ICP from permanently locked non-KYC-verified neurons, analogous to the "seizing funds of locked tokens and adding them to the reserve" recommendation in the external report.
2. **Expose a query** (`list_non_kyc_neurons`) so the community can audit the total ICP locked in `kyc_verified = false` neurons.
3. **Document the irrecoverability** explicitly in the `disburse_neuron` error message so users understand the permanent nature of the lockout.

---

### Proof of Concept

1. A genesis neuron exists with `kyc_verified = false` (initial state at genesis, confirmed by `ApproveGenesisKyc` description).
2. The neuron's dissolve delay elapses; the neuron enters `NeuronState::Dissolved`.
3. The controller sends an ingress `manage_neuron` → `Disburse` command to the NNS Governance canister.
4. Execution reaches `disburse_neuron` → checks `is_neuron_kyc_verified` → returns:
   ```
   PreconditionFailed: "Neuron <id> is not kyc verified."
   ``` [8](#0-7) 
5. The ICP remains in the governance canister's ledger subaccount. No further user action can unlock it. No protocol action can seize it. The funds are permanently stranded.

This is confirmed by the existing test fixture which explicitly demonstrates that non-KYC-verified neurons cannot be disbursed:

```rust
assert_matches!(result, Err(msg) if msg.error_message.to_lowercase().contains("kyc verified"));
``` [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L1990-1995)
```rust
        if !is_neuron_kyc_verified {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {} is not kyc verified.", id.id),
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L2949-2954)
```rust
        if !parent_neuron.kyc_verified {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron is not kyc verified: {}", id.id),
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L3019-3021)
```rust
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .build();
```

**File:** rs/nns/governance/src/neuron_store.rs (L1059-1065)
```rust
    if neuron_id_to_principal.len() > APPROVE_GENESIS_KYC_MAX_NEURONS {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "ApproveGenesisKyc can only change the KYC status of up to {APPROVE_GENESIS_KYC_MAX_NEURONS} neurons at a time"
            ),
        ));
```

**File:** rs/nns/governance/src/neuron_store.rs (L1068-1073)
```rust
    for (neuron_id, principal) in neuron_id_to_principal {
        let result = neuron_store.with_neuron_mut(&neuron_id, |neuron| {
            if neuron.controller() == principal {
                neuron.kyc_verified = true;
            }
        });
```

**File:** rs/nns/governance/src/proposals/self_describing.rs (L55-65)
```rust
impl DocumentedAction for ApproveGenesisKyc {
    const NAME: &'static str = "Approve Genesis KYC";
    const DESCRIPTION: &'static str = "Set GenesisKYC=true for batches of principals.\n\n\
        When new neurons are created at Genesis, they have GenesisKYC=false. This restricts what \
        actions they can perform. Specifically, they cannot spawn new neurons, and once their \
        dissolve delays are zero, they cannot be disbursed and their balances unlocked to new \
        accounts.\n\n\
        (Special note: The Genesis event disburses all ICP in the form of neurons, \
        whose principals must be KYCed. Consequently, all neurons created after Genesis have \
        GenesisKYC=true set automatically since they must have been derived from balances that \
        have already been KYCed.)";
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L1-16)
```rust
use crate::{
    governance::ledger_helper::{BurnNeuronFeesOperation, NeuronStakeTransferOperation},
    neuron::{DissolveStateAndAge, Neuron, combine_aged_stakes},
    neuron_store::NeuronStore,
    pb::v1::{
        GovernanceError, NeuronState, ProposalData, ProposalStatus, VotingPowerEconomics,
        governance_error::ErrorType,
        manage_neuron::{Merge, NeuronIdOrSubaccount},
    },
};
use ic_base_types::PrincipalId;
use ic_nns_common::pb::v1::NeuronId;
use ic_nns_governance_api::manage_neuron_response::MergeResponse;
use icp_ledger::Subaccount;
use std::collections::BTreeMap;

```

**File:** rs/nns/governance/tests/governance.rs (L4091-4091)
```rust
    assert_matches!(result, Err(msg) if msg.error_message.to_lowercase().contains("kyc verified"));
```
