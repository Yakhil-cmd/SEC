### Title
`ApproveGenesisKyc` Proposal Execution Permanently Blocked via Hotkey Inflation Griefing - (File: `rs/nns/governance/src/neuron_store.rs`)

### Summary
The `approve_genesis_kyc` function counts neurons using `get_neuron_ids_readable_by_caller`, which includes neurons where the target principal is merely a **hotkey** (not the controller). An attacker who controls many neurons can add a target principal as a hotkey to 1001+ of their own neurons, inflating the count beyond the hard 1000-neuron cap and causing the `ApproveGenesisKyc` proposal execution to permanently fail — with no neurons KYC'd and no retry possible for the same principal set.

### Finding Description
In `rs/nns/governance/src/neuron_store.rs`, `approve_genesis_kyc` builds its neuron count by calling `get_neuron_ids_readable_by_caller(principal)` for each principal in the proposal: [1](#0-0) 

`get_neuron_ids_readable_by_caller` returns **all** neurons where the principal is either the controller **or a hotkey**, as confirmed by the test: [2](#0-1) 

If the resulting map exceeds 1000 entries, the entire proposal execution is rejected and **no** neurons are KYC'd: [3](#0-2) 

However, the actual KYC mutation only fires for neurons where the principal is the **controller**, not a hotkey: [4](#0-3) 

This creates a mismatch: hotkey-linked neurons are counted toward the cap but never KYC'd. An attacker who already holds 1001+ neurons can add any target principal `P` as a hotkey to those neurons. When an `ApproveGenesisKyc` proposal listing `P` is executed, the count for `P` exceeds 1000 and the proposal fails — even though `P` may control far fewer than 1000 neurons of their own.

The proposal execution path is: [5](#0-4) 

### Impact Explanation
The `ApproveGenesisKyc` proposal fails with `PreconditionFailed`, and **no** neurons in the batch are KYC-verified. Genesis neurons with `kyc_verified = false` cannot spawn child neurons and cannot disburse their stake once dissolved. A griefer can permanently block KYC approval for any principal by maintaining the hotkey relationship, forcing the NNS to either submit a new proposal excluding the targeted principal or wait for the attacker to remove the hotkeys.

### Likelihood Explanation
Moderate-low. The attacker must already hold 1001+ neurons (minimum 1 ICP each). However, large ICP holders exist on mainnet, the NNS voting period is 4 days (ample time to add hotkeys after a proposal is submitted), and adding a hotkey to a neuron is a cheap, permissionless operation requiring no consent from the hotkey principal. The `ApproveGenesisKyc` action is rare but remains a live governance action type.

### Recommendation
Replace `get_neuron_ids_readable_by_caller` with a lookup that returns only neurons **controlled** by the principal (i.e., where `neuron.controller() == principal`). The count used to enforce the 1000-neuron cap should match the set of neurons that will actually be mutated, eliminating the hotkey-inflation vector. Alternatively, filter the collected map to only include entries where the principal is the controller before applying the cap check.

### Proof of Concept
1. Attacker holds 1001 neurons, each with their own principal as controller.
2. NNS submits `ApproveGenesisKyc { principals: [P] }` for a genesis principal `P`.
3. Attacker calls `manage_neuron` → `AddHotKey { new_hot_key: P }` on all 1001 neurons (cheap, no consent from `P` required).
4. Proposal reaches execution: `get_neuron_ids_readable_by_caller(P)` returns 1001 neuron IDs (attacker's neurons via hotkey + P's own neurons).
5. `neuron_id_to_principal.len() > 1000` → `Err(PreconditionFailed)` → proposal marked failed, `P`'s neurons remain `kyc_verified = false`.
6. Attacker repeats step 3 for any subsequent retry proposal listing `P`, indefinitely blocking KYC at negligible ongoing cost.

### Citations

**File:** rs/nns/governance/src/neuron_store.rs (L1048-1057)
```rust
    let principal_set: HashSet<PrincipalId> = principals.iter().cloned().collect();
    let neuron_id_to_principal = principal_set
        .into_iter()
        .flat_map(|principal| {
            neuron_store
                .get_neuron_ids_readable_by_caller(principal)
                .into_iter()
                .map(move |neuron_id| (neuron_id, principal))
        })
        .collect::<HashMap<_, _>>();
```

**File:** rs/nns/governance/src/neuron_store.rs (L1059-1066)
```rust
    if neuron_id_to_principal.len() > APPROVE_GENESIS_KYC_MAX_NEURONS {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "ApproveGenesisKyc can only change the KYC status of up to {APPROVE_GENESIS_KYC_MAX_NEURONS} neurons at a time"
            ),
        ));
    }
```

**File:** rs/nns/governance/src/neuron_store.rs (L1068-1078)
```rust
    for (neuron_id, principal) in neuron_id_to_principal {
        let result = neuron_store.with_neuron_mut(&neuron_id, |neuron| {
            if neuron.controller() == principal {
                neuron.kyc_verified = true;
            }
        });
        // Log errors but continue with the rest of the neurons.
        if let Err(e) = result {
            eprintln!("{LOG_PREFIX}ERROR: Failed to approve KYC for neuron {neuron_id:?}: {e:?}");
        }
    }
```

**File:** rs/nns/governance/src/neuron_store/neuron_store_tests.rs (L689-736)
```rust
fn test_get_non_empty_neuron_ids_readable_by_caller() {
    // Prepare the neurons.
    let controller = PrincipalId::new_user_test_id(1);
    let hot_key = PrincipalId::new_user_test_id(2);
    let neuron_builder = |i| {
        simple_neuron_builder(i)
            .with_controller(controller)
            .with_hot_keys(vec![hot_key])
    };
    let neuron_empty = neuron_builder(1).build();
    let neuron_empty_with_fees = neuron_builder(2)
        .with_cached_neuron_stake_e8s(1)
        .with_neuron_fees_e8s(1)
        .build();
    let neuron_with_stake = neuron_builder(3).with_cached_neuron_stake_e8s(1).build();
    let neuron_with_maturity = neuron_builder(4).with_maturity_e8s_equivalent(1).build();
    let neuron_with_staked_maturity = neuron_builder(5)
        .with_staked_maturity_e8s_equivalent(1)
        .build();
    let neuron_with_maturity_disbursement = neuron_builder(6)
        .with_maturity_disbursements_in_progress(vec![MaturityDisbursement {
            finalize_disbursement_timestamp_seconds: 1,
            ..Default::default()
        }])
        .build();
    let neuron_store = NeuronStore::new(btreemap! {
        1 => neuron_empty,
        2 => neuron_empty_with_fees,
        3 => neuron_with_stake,
        4 => neuron_with_maturity,
        5 => neuron_with_staked_maturity,
        6 => neuron_with_maturity_disbursement,
    });

    assert_eq!(
        neuron_store.get_non_empty_neuron_ids_readable_by_caller(controller),
        btreeset! { 3, 4, 5, 6 }
            .into_iter()
            .map(NeuronId::from_u64)
            .collect()
    );
    assert_eq!(
        neuron_store.get_non_empty_neuron_ids_readable_by_caller(hot_key),
        btreeset! { 3, 4, 5, 6 }
            .into_iter()
            .map(NeuronId::from_u64)
            .collect()
    );
```

**File:** rs/nns/governance/src/governance.rs (L4226-4229)
```rust
            ValidProposalAction::ApproveGenesisKyc(proposal) => {
                let result = self.approve_genesis_kyc(&proposal.principals);
                self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
            }
```
