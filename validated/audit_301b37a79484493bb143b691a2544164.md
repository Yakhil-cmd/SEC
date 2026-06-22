### Title
`ManageNervousSystemParameters` Updates `transaction_fee_e8s` in SNS Governance Without Propagating to the Ledger Canister - (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

SNS Governance exposes two separate proposal types that can affect the ledger transfer fee: `ManageNervousSystemParameters` (Action ID 2) and `ManageLedgerParameters` (Action ID 13). Only `ManageLedgerParameters` actually upgrades the ledger canister and then back-syncs governance's cached copy. `ManageNervousSystemParameters`, when it changes `transaction_fee_e8s`, updates only the governance canister's local `NervousSystemParameters` state and never notifies or upgrades the ledger. This creates a persistent divergence between the fee value governance uses for all ledger transfer calculations and the fee the ledger actually enforces.

---

### Finding Description

`perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs` is the execution handler for `ManageNervousSystemParameters` proposals. It applies the proposed parameters and writes them to `self.proto.parameters`: [1](#0-0) 

There is no call to the ledger canister here. The function is synchronous and makes no inter-canister calls.

By contrast, `perform_manage_ledger_parameters` (the handler for `ManageLedgerParameters`) upgrades the ledger canister with the new fee and then, only after confirming the upgrade succeeded, back-syncs governance's cached `transaction_fee_e8s`: [2](#0-1) 

The governance canister reads its cached fee via `transaction_fee_e8s_or_panic()` for every ledger transfer it initiates: [3](#0-2) 

This cached value is used directly in `disburse`, `split`, and `disburse_to_neuron` to compute the amount sent to the ledger and the fee argument passed to `transfer_funds`. For example, in `split`: [4](#0-3) 

And in `disburse`: [5](#0-4) 

The `NervousSystemParameters` proto field `transaction_fee_e8s` is documented as the ledger's fee: [6](#0-5) 

The integration test for `ManageNervousSystemParameters` confirms that only the governance-side parameter is checked after the proposal executes — the ledger fee is never verified to match: [7](#0-6) 

---

### Impact Explanation

After a `ManageNervousSystemParameters` proposal changes `transaction_fee_e8s`, the governance canister's cached fee diverges from the actual ledger fee. Two concrete outcomes follow:

1. **Cached fee set lower than actual ledger fee**: Governance calls `transfer_funds(amount, cached_fee, ...)` but the ledger requires a higher fee. The ledger rejects the transfer. All `disburse`, `split`, and `disburse_to_neuron` operations fail for all neurons in the SNS. Neuron holders cannot unlock their staked tokens.

2. **Cached fee set higher than actual ledger fee**: Governance subtracts the inflated cached fee from the neuron's stake before calling the ledger. Users are overcharged on every disburse/split operation, permanently losing the difference between the inflated cached fee and the real ledger fee.

Both outcomes affect all users of the SNS, not just the proposer.

---

### Likelihood Explanation

Any SNS token holder with sufficient voting power can submit a `ManageNervousSystemParameters` proposal. This is the standard, documented governance entry path — no privileged key or admin role is required beyond normal token-weighted voting. The divergence can be triggered accidentally (a community that does not understand the two-proposal distinction) or deliberately (a token holder who accumulates majority voting power). The `ManageLedgerParameters` proposal exists as the correct path for changing the ledger fee, but nothing in the code or proposal validation prevents `ManageNervousSystemParameters` from setting `transaction_fee_e8s` to an arbitrary value that conflicts with the actual ledger state. [8](#0-7) 

---

### Recommendation

1. **Remove `transaction_fee_e8s` from the set of fields that `ManageNervousSystemParameters` can modify**, or add a validation step that rejects proposals that attempt to change it via this path. The correct and only supported path for changing the ledger fee is `ManageLedgerParameters`, which performs the actual ledger upgrade and then syncs governance's copy.

2. Alternatively, if `ManageNervousSystemParameters` must be allowed to set `transaction_fee_e8s`, add an async call to the ledger canister (e.g., `icrc1_fee`) to verify the proposed value matches the actual ledger fee before accepting the proposal, and reject proposals where they diverge.

3. Add a proposal validation check in `validate_and_render_manage_nervous_system_parameters` that explicitly rejects any proposal that sets `transaction_fee_e8s` to a value different from the current ledger fee.

---

### Proof of Concept

1. Deploy an SNS with default parameters (`transaction_fee_e8s = 10_000`, ledger fee = 10_000).
2. Submit and pass a `ManageNervousSystemParameters` proposal with `transaction_fee_e8s: Some(1)`.
3. `perform_manage_nervous_system_parameters` executes, setting `self.proto.parameters.transaction_fee_e8s = Some(1)`. No ledger call is made. The ledger still enforces fee = 10_000.
4. A neuron holder calls `disburse`. Governance computes `disburse_amount_e8s -= 1` (using cached fee = 1) and calls `transfer_funds(amount, 1, ...)`.
5. The ledger rejects the transfer because the provided fee (1) is less than the required fee (10_000).
6. The `disburse` call returns an error. The neuron holder cannot retrieve their staked tokens.
7. This affects every neuron holder in the SNS simultaneously. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2025-2027)
```rust
        }

        self.closest_proposal_deadline_timestamp_seconds = self
```

**File:** rs/sns/governance/src/governance.rs (L2144-2146)
```rust
            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
```

**File:** rs/sns/governance/src/governance.rs (L2278-2287)
```rust
                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
                }
            }
            Err(msg) => {
```

**File:** rs/sns/governance/src/governance.rs (L2581-2617)
```rust
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3090-3096)
```rust
    async fn perform_manage_ledger_parameters(
        &mut self,
        proposal_id: u64,
        manage_ledger_parameters: ManageLedgerParameters,
    ) -> Result<(), GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;

```

**File:** rs/sns/governance/src/governance.rs (L3189-3196)
```rust
                    // success
                    // update nervous-system-parameters transaction_fee if the fee is changed.
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
                    return Ok(());
```

**File:** rs/sns/governance/src/governance.rs (L3368-3373)
```rust
    /// Returns the ledger's transaction fee as stored in the service nervous parameters.
    pub(crate) fn transaction_fee_e8s_or_panic(&self) -> u64 {
        self.nervous_system_parameters_or_panic()
            .transaction_fee_e8s
            .expect("NervousSystemParameters must have transaction_fee_e8s")
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1129-1131)
```text
  // The transaction fee that must be paid for ledger transactions (except
  // minting and burning governance tokens).
  optional uint64 transaction_fee_e8s = 3;
```

**File:** rs/sns/integration_tests/src/proposals.rs (L186-217)
```rust
            let proposal_payload = Proposal {
                title: "Test valid ManageNervousSystemParameters proposal".into(),
                action: Some(Action::ManageNervousSystemParameters(
                    NervousSystemParameters {
                        transaction_fee_e8s: Some(120_001),
                        neuron_minimum_stake_e8s: Some(398_002_900),
                        ..Default::default()
                    },
                )),
                ..Default::default()
            };

            // Submit a proposal. It should then be executed because the submitter
            // has a majority stake and submitting also votes automatically.
            let proposal_id = sns_canisters
                .make_proposal(&user, &subaccount, proposal_payload)
                .await
                .unwrap();

            let proposal = sns_canisters.get_proposal(proposal_id).await;

            assert_eq!(proposal.action, 2);
            assert_ne!(proposal.decided_timestamp_seconds, 0);
            assert_ne!(proposal.executed_timestamp_seconds, 0);

            let live_sys_params: NervousSystemParameters = sns_canisters
                .governance
                .query_("get_nervous_system_parameters", candid_one, ())
                .await?;

            assert_eq!(live_sys_params.transaction_fee_e8s, Some(120_001));
            assert_eq!(live_sys_params.neuron_minimum_stake_e8s, Some(398_002_900));
```
