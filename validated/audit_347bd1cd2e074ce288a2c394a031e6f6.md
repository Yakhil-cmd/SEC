### Title
SNS Governance `ManageNervousSystemParameters` Updates `transaction_fee_e8s` Without Syncing the SNS Ledger Fee - (`rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS system maintains the transfer fee in two separate state stores: `NervousSystemParameters.transaction_fee_e8s` inside the SNS governance canister, and the actual `transfer_fee` inside the SNS ledger canister. The `ManageLedgerParameters` proposal path correctly updates both stores. However, the `ManageNervousSystemParameters` proposal path updates only the governance copy (`NervousSystemParameters.transaction_fee_e8s`) without touching the ledger's actual fee, creating a persistent divergence between the two stores. Governance then uses its stale copy for all fee arithmetic in neuron operations, while the ledger enforces the real fee, causing all governance-initiated ledger transfers to fail or produce incorrect amounts.

---

### Finding Description

The SNS governance canister stores a copy of the ledger transfer fee in `NervousSystemParameters.transaction_fee_e8s`: [1](#0-0) 

This value is the authoritative source used by governance for all fee arithmetic: [2](#0-1) 

The `ManageLedgerParameters` proposal path correctly keeps both stores in sync. After upgrading the ledger with the new fee, it explicitly back-propagates the new fee into governance state: [3](#0-2) 

However, the `ManageNervousSystemParameters` proposal path (`perform_manage_nervous_system_parameters`) updates `self.proto.parameters` — which contains `transaction_fee_e8s` — without performing any corresponding ledger upgrade or ledger fee update: [4](#0-3) 

The validation for `transaction_fee_e8s` only checks that the field is present, not that it matches the ledger's actual fee: [5](#0-4) 

An integration test confirms that `ManageNervousSystemParameters` can freely set `transaction_fee_e8s` to an arbitrary value (e.g., `120_001`) with no ledger interaction: [6](#0-5) 

After such a proposal executes, `NervousSystemParameters.transaction_fee_e8s` in governance diverges from the ledger's actual `transfer_fee`. Every subsequent governance-initiated ledger call (disburse, fee burn) will use the wrong fee value from `transaction_fee_e8s_or_panic()`, while the ICRC-1 ledger enforces an exact fee match.

---

### Impact Explanation

The governance-stored `transaction_fee_e8s` is used directly in neuron disburse calculations: [7](#0-6) [8](#0-7) 

If `transaction_fee_e8s` in governance is set lower than the ledger's actual fee, governance will pass an insufficient fee to `transfer_funds`, and the ICRC-1 ledger will reject the transfer with `BadFee`. All neuron disbursements and fee-burn operations initiated by governance will fail until the discrepancy is manually corrected via another proposal.

If `transaction_fee_e8s` is set higher than the ledger's actual fee, governance will over-deduct from the disburse amount, causing users to receive fewer tokens than they are entitled to.

The `ManageLedgerParameters` integration test confirms that the ledger enforces an exact fee match: [9](#0-8) 

---

### Likelihood Explanation

Any SNS neuron holder can submit a `ManageNervousSystemParameters` proposal. If the proposal passes (majority vote among SNS token holders, which is a "ledger/governance/chain-fusion user" per the scope), `transaction_fee_e8s` is updated in governance without touching the ledger. This can happen accidentally (e.g., a community wanting to update governance parameters while inadvertently setting a mismatched fee) or deliberately. The `ManageLedgerParameters` path exists precisely to change the ledger fee, but nothing prevents `ManageNervousSystemParameters` from setting a conflicting value in governance state.

---

### Recommendation

1. Remove `transaction_fee_e8s` from the set of fields that `ManageNervousSystemParameters` is permitted to change, since the ledger is the authoritative source of truth for the fee. Any fee change must go through `ManageLedgerParameters`, which upgrades the ledger and then back-propagates the new value.
2. Alternatively, add a validation step in `perform_manage_nervous_system_parameters` that rejects any proposed `transaction_fee_e8s` that does not match the ledger's current fee (queried via `icrc1_fee`).
3. Add an invariant check (e.g., in heartbeat or post-upgrade) that asserts `NervousSystemParameters.transaction_fee_e8s == ledger.icrc1_fee()` and emits a metric or log warning on divergence.

---

### Proof of Concept

1. Deploy an SNS with default `transaction_fee_e8s = 10_000` and ledger `transfer_fee = 10_000`.
2. Submit and pass a `ManageNervousSystemParameters` proposal setting `transaction_fee_e8s = 1`.
3. `perform_manage_nervous_system_parameters` updates `self.proto.parameters.transaction_fee_e8s = 1` with no ledger interaction.
4. The ledger still enforces `transfer_fee = 10_000`.
5. Attempt to disburse a neuron. Governance calls `transfer_funds(amount - 1, 1, ...)` (using the governance-stored fee of `1`). The ICRC-1 ledger returns `BadFee { expected_fee: 10_000 }`.
6. The disburse operation fails. All neuron disbursements are now broken until a corrective proposal is passed. [4](#0-3) [3](#0-2) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1658-1661)
```rust
    /// The transaction fee that must be paid for ledger transactions (except
    /// minting and burning governance tokens).
    #[prost(uint64, optional, tag = "3")]
    pub transaction_fee_e8s: ::core::option::Option<u64>,
```

**File:** rs/sns/governance/src/governance.rs (L1168-1172)
```rust
        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/sns/governance/src/governance.rs (L1181-1191)
```rust
        if max_burnable_fee > transaction_fee_e8s {
            let _result = self
                .ledger
                .transfer_funds(
                    max_burnable_fee,
                    0, // Burning transfers don't pay a fee.
                    Some(from_subaccount),
                    self.governance_minting_account(),
                    self.env.now(),
                )
                .await?;
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

**File:** rs/sns/governance/src/types.rs (L620-624)
```rust
    /// Validates that the nervous system parameter transaction_fee_e8s is well-formed.
    fn validate_transaction_fee_e8s(&self) -> Result<u64, String> {
        self.transaction_fee_e8s
            .ok_or_else(|| "NervousSystemParameters.transaction_fee_e8s must be set".to_string())
    }
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

**File:** rs/sns/integration_tests/src/manage_ledger_parameters.rs (L77-101)
```rust
    // check that the fee on the ledger has changed.
    let ledger_fee_after_proposal = icrc1_fee(&state_machine, sns_canisters.ledger_canister_id);

    assert!(ledger_fee_after_proposal.0.to_u64().unwrap() != DEFAULT_LEDGER_TRANSFER_FEE);
    assert!(ledger_fee_after_proposal.0.to_u64().unwrap() == new_fee);

    // try making transfers using the new fee and the old fee.
    icrc1_transfer(
        &state_machine,
        sns_canisters.ledger_canister_id,
        user,
        TransferArg {
            amount: Nat::from(5_u8),
            fee: Some(Nat::from(new_fee)),
            from_subaccount: None,
            to: Account {
                owner: Principal::management_canister(),
                subaccount: None,
            },
            memo: None,
            created_at_time: None,
        },
    )
    .expect("This transfer with the new fee must succeed");

```
