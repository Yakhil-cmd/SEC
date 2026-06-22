### Title
Parallel Parameter Values in SNS Swap `Init` and SNS Governance/Ledger May Cause Inconsistent Swap Finalization - (File: rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto)

### Summary
The SNS Swap canister's `Init` struct stores `transaction_fee_e8s` and `neuron_minimum_stake_e8s` as independent copies of values that must match those held in the SNS Ledger and SNS Governance canisters respectively. The protocol's own documentation explicitly acknowledges these copies are never cross-validated. If the SNS Governance updates its `NervousSystemParameters` via a governance proposal after the Swap is initialized, the Swap canister retains stale values, causing its participant-validation logic to diverge from the actual protocol parameters.

### Finding Description

The `Init` message in the SNS Swap canister contains two fields that are documented as duplicates of values held elsewhere: [1](#0-0) 

The comments read verbatim:
- `transaction_fee_e8s`: *"Same as SNS ledger. Must hold the same value as SNS ledger. Whether the values match is not checked. If they don't match things will break."*
- `neuron_minimum_stake_e8s`: *"Same as SNS governance. Must hold the same value as SNS governance. Whether the values match is not checked. If they don't match things will break."*

The Rust-generated struct mirrors this: [2](#0-1) 

The `Init` struct is explicitly immutable after canister creation: [3](#0-2) 

Meanwhile, the SNS Governance canister stores the same parameters in its mutable `NervousSystemParameters`: [4](#0-3) 

The Swap canister's `Init::validate` only checks that the fields are present, not that they match the live governance/ledger values: [5](#0-4) 

The Swap canister uses these stale values in `Params::validate` to enforce that participants will receive enough tokens to form neurons: [6](#0-5) 

The SNS Governance's `NervousSystemParameters` can be changed at any time via a governance proposal, while the Swap's `Init` copy cannot be updated. These two stores are set independently at initialization time: [7](#0-6) [8](#0-7) 

### Impact Explanation

If the SNS community passes a governance proposal to increase `neuron_minimum_stake_e8s` in `NervousSystemParameters` after the Swap is initialized but before it finalizes, the Swap canister continues to validate participants against the old (lower) minimum. Participants who contribute enough ICP to receive tokens above the stale minimum but below the updated governance minimum will be accepted by the Swap. When the Swap finalizes and instructs the SNS Governance to create neurons for these participants, the governance will reject neuron creation because the stake falls below its current minimum. This breaks swap finalization for affected participants, potentially locking their ICP contributions in an unresolvable state. The inverse (decreasing the minimum) causes the Swap to reject participants who would actually be valid under the updated governance parameters.

### Likelihood Explanation

Likelihood is low. The divergence requires an SNS governance proposal to change `neuron_minimum_stake_e8s` or `transaction_fee_e8s` to pass during the active swap window (typically days to weeks). However, the risk is non-zero: SNS communities may legitimately wish to adjust parameters and may not be aware that the Swap canister holds an immutable, unvalidated copy. The protocol's own comments acknowledge the risk explicitly, indicating the developers are aware of the design flaw but have not enforced consistency.

### Recommendation

At Swap initialization, cross-validate `transaction_fee_e8s` against the live SNS Ledger transfer fee and `neuron_minimum_stake_e8s` against the live SNS Governance `NervousSystemParameters` via inter-canister query calls. Alternatively, remove these fields from `Init` entirely and have the Swap canister query the authoritative source (SNS Ledger and SNS Governance) at finalization time rather than relying on a cached copy set at initialization.

### Proof of Concept

1. Deploy an SNS with `neuron_minimum_stake_e8s = 100_000_000` in both `SnsInitPayload` (which populates both the Governance `NervousSystemParameters` and the Swap `Init`).
2. The Swap opens. The Swap's `Init.neuron_minimum_stake_e8s` is now immutably `100_000_000`.
3. The SNS community submits and passes a governance proposal (`ManageNervousSystemParameters`) setting `neuron_minimum_stake_e8s = 200_000_000` in the SNS Governance.
4. A participant contributes ICP sufficient to receive `150_000_000` SNS tokens per neuron basket slot. The Swap's `Params::validate` accepts this (150M > 100M stale minimum).
5. The Swap commits. During finalization, the Swap instructs SNS Governance to create neurons with `150_000_000` tokens each. The Governance rejects these because `150_000_000 < 200_000_000` (the current minimum).
6. Swap finalization fails for these participants. Their ICP contributions are committed but no SNS neurons are created, resulting in loss of expected token allocation. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L314-320)
```text
  // Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
  // values match is not checked. If they don't match things will break.
  optional uint64 transaction_fee_e8s = 13;

  // Same as SNS governance. Must hold the same value as SNS governance. Whether
  // the values match is not checked. If they don't match things will break.
  optional uint64 neuron_minimum_stake_e8s = 14;
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L243-248)
```rust
/// The initialisation data of the canister. Always specified on
/// canister creation, and cannot be modified afterwards.
///
/// If the initialization parameters are incorrect, the swap will
/// immediately be aborted.
#[derive(
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L282-290)
```rust
    /// Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
    /// values match is not checked. If they don't match things will break.
    #[prost(uint64, optional, tag = "13")]
    pub transaction_fee_e8s: ::core::option::Option<u64>,
    /// Same as SNS governance. Must hold the same value as SNS governance. Whether
    /// the values match is not checked. If they don't match things will break.
    #[prost(uint64, optional, tag = "14")]
    pub neuron_minimum_stake_e8s: ::core::option::Option<u64>,
    /// An optional text that swap participants should confirm before they may
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1123-1132)
```text
  // The minimum number of e8s (10e-8 of a token) that can be staked in a neuron.
  //
  // To ensure that staking and disbursing of the neuron work, the chosen value
  // must be larger than the transaction_fee_e8s.
  optional uint64 neuron_minimum_stake_e8s = 2;

  // The transaction fee that must be paid for ledger transactions (except
  // minting and burning governance tokens).
  optional uint64 transaction_fee_e8s = 3;

```

**File:** rs/sns/swap/src/types.rs (L282-316)
```rust
    pub fn validate(&self) -> Result<(), String> {
        validate_canister_id(&self.nns_governance_canister_id)?;
        validate_canister_id(&self.sns_governance_canister_id)?;
        validate_canister_id(&self.sns_ledger_canister_id)?;
        validate_canister_id(&self.icp_ledger_canister_id)?;
        validate_canister_id(&self.sns_root_canister_id)?;

        if self.fallback_controller_principal_ids.is_empty() {
            return Err("at least one fallback controller required".to_string());
        }
        for fc in &self.fallback_controller_principal_ids {
            validate_principal(fc)?;
        }

        if self.transaction_fee_e8s.is_none() {
            // The value itself is not checked; only that it is supplied. Needs to
            // match the value in SNS ledger though.
            return Err("transaction_fee_e8s is required.".to_string());
        }

        if self.neuron_minimum_stake_e8s.is_none() {
            // As with transaction_fee_e8s, the value itself is not checked; only
            // that it is supplied. Needs to match the value in SNS governance
            // though.
            return Err("neuron_minimum_stake_e8s is required.".to_string());
        }

        self.validate_swap_init_for_one_proposal_flow()?;

        if self.should_auto_finalize.is_none() {
            return Err("should_auto_finalize is required.".to_string());
        }

        Ok(())
    }
```

**File:** rs/sns/swap/src/types.rs (L332-351)
```rust
        let transaction_fee_e8s = init
            .transaction_fee_e8s
            .expect("transaction_fee_e8s was not supplied.");

        let neuron_minimum_stake_e8s = init
            .neuron_minimum_stake_e8s
            .expect("neuron_minimum_stake_e8s was not supplied");

        let neuron_basket_count = self
            .neuron_basket_construction_parameters
            .as_ref()
            .expect("participant_neuron_basket not populated.")
            .count as u128;

        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
```

**File:** rs/sns/test_utils/src/itest_helpers.rs (L247-260)
```rust
        let swap = SwapInit {
            fallback_controller_principal_ids: vec![
                PrincipalId::new_user_test_id(6360).to_string(),
            ],
            should_auto_finalize: Some(true),
            transaction_fee_e8s: Some(self.ledger.transfer_fee.0.to_u64().unwrap()),
            neuron_minimum_stake_e8s: Some(
                governance
                    .parameters
                    .as_ref()
                    .unwrap()
                    .neuron_minimum_stake_e8s
                    .unwrap(),
            ),
```

**File:** rs/sns/test_utils/src/itest_helpers.rs (L324-330)
```rust
    // Governance canister_init args.
    {
        let governance = &mut sns_canister_init_payloads.governance;
        governance.ledger_canister_id = Some(ledger_canister_id);
        governance.root_canister_id = Some(root_canister_id);
        governance.swap_canister_id = Some(swap_canister_id);
    }
```

**File:** rs/sns/init/src/lib.rs (L697-699)
```rust
            transaction_fee_e8s: self.transaction_fee_e8s,
            neuron_minimum_stake_e8s: self.neuron_minimum_stake_e8s,
            confirmation_text: self.confirmation_text.clone(),
```
