### Title
Incomplete Cross-Canister Value Validation in SNS Swap `Init` Allows Mismatched Fee/Stake Parameters to Break Swap Finalization — (File: `rs/sns/swap/src/types.rs`)

### Summary
The `Init::validate()` function in the SNS Swap canister only checks that `transaction_fee_e8s` and `neuron_minimum_stake_e8s` are *present* (not `None`), but explicitly does **not** validate that these values match the actual values configured in the SNS Ledger and SNS Governance canisters respectively. The proto definition itself documents this gap. If the values diverge — whether through a post-deployment SNS governance proposal changing the ledger fee, or through a crafted deployment payload — the swap finalization logic will silently use stale/incorrect parameters, causing ledger transfers to fail and locking up participant ICP.

### Finding Description

The `Init` struct in the SNS Swap canister stores copies of two critical parameters:

- `transaction_fee_e8s`: "Same as SNS ledger. Must hold the same value as SNS ledger. **Whether the values match is not checked. If they don't match things will break.**"
- `neuron_minimum_stake_e8s`: "Same as SNS governance. Must hold the same value as SNS governance. **Whether the values match is not checked. If they don't match things will break.**" [1](#0-0) 

The `validate()` function in `rs/sns/swap/src/types.rs` only checks that these fields are `Some(...)`, not that their values are consistent with the actual deployed SNS Ledger or SNS Governance canister:

```rust
if self.transaction_fee_e8s.is_none() {
    return Err("transaction_fee_e8s is required.".to_string());
}
if self.neuron_minimum_stake_e8s.is_none() {
    return Err("neuron_minimum_stake_e8s is required.".to_string());
}
``` [2](#0-1) 

The `validate_canister_id` helper used for the five canister ID fields similarly only checks that the string parses as a valid `PrincipalId`, not that the canister actually exists or is of the correct type: [3](#0-2) 

The `Init` struct is declared immutable after creation: [4](#0-3) 

This means if the SNS Ledger's transaction fee is changed via an SNS governance proposal after the Swap canister is initialized, the Swap canister will continue using the stale fee value from its `Init` for all neuron basket computations during finalization.

### Impact Explanation

During swap finalization, `sweep_sns` and `create_sns_neuron_recipes` use `init.transaction_fee_e8s_or_panic()` to compute the amounts transferred to each neuron. If the actual SNS Ledger fee is higher than the value stored in `Init`, every ledger transfer during finalization will be rejected by the ledger (insufficient fee), causing the entire finalization to fail. Participant ICP contributions are locked in the swap canister until the situation is resolved (if ever), constituting a **ledger conservation bug** and a **chain-fusion mint/burn/replay bug** in the SNS token distribution path. [5](#0-4) 

### Likelihood Explanation

The attack path requires two steps that are individually plausible:

1. An SNS is deployed with a low `transaction_fee_e8s` (e.g., 1 e8).
2. Before the swap finalizes, the SNS governance passes a proposal to raise the SNS Ledger transaction fee (a routine governance action). The Swap canister's `Init` is immutable and cannot be updated to reflect the new fee.

Alternatively, a malicious SNS deployer can intentionally set `transaction_fee_e8s` in the `SnsInitPayload` to a value that differs from the `transaction_fee` in the `LedgerParameters` section of the same payload. The `SnsInitPayload` validation in `rs/sns/init/src/lib.rs` only checks that `transaction_fee_e8s` is `Some(_)` — it does not cross-validate the Swap's copy against the Ledger's copy: [6](#0-5) 

### Recommendation

1. At swap finalization time, query the actual SNS Ledger canister for its current `transfer_fee` and compare it against `init.transaction_fee_e8s`. Abort finalization with a clear error if they diverge.
2. Similarly, query the SNS Governance canister for its current `neuron_minimum_stake_e8s` before creating neuron recipes.
3. Add a cross-validation step in `Init::validate()` (or in `Swap::new()`) that performs an inter-canister call to verify these values match at initialization time.
4. Remove the explicit documentation comment "Whether the values match is not checked" and replace it with an enforced invariant.

### Proof of Concept

**Step 1**: Deploy an SNS with `transaction_fee_e8s = 1` in the `SnsInitPayload` but configure the SNS Ledger with `transaction_fee = 10_000`. The `Init::validate()` call in `Swap::new()` succeeds because `transaction_fee_e8s` is `Some(1)`. [7](#0-6) 

**Step 2**: Participants contribute ICP; the swap reaches `Committed` state.

**Step 3**: `finalize_swap` is called. `sweep_sns` calls `create_sns_neuron_recipes`, which uses `init.transaction_fee_e8s_or_panic()` returning `1`. The computed transfer amounts are `amount_e8s - 1` per neuron.

**Step 4**: The SNS Ledger rejects every transfer because the actual fee is `10_000`, not `1`. All transfers fail, `sweep_sns` returns a `SweepResult` with all entries in `failure`. The swap is stuck in `Committed` state with participant ICP locked. [8](#0-7)

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

**File:** rs/sns/swap/src/types.rs (L37-44)
```rust
pub fn validate_canister_id(p: &str) -> Result<(), String> {
    let _pp = PrincipalId::from_str(p).map_err(|x| {
        format!(
            "Couldn't validate CanisterId. String \"{p}\" could not be converted to PrincipalId: {x}"
        )
    })?;
    Ok(())
}
```

**File:** rs/sns/swap/src/types.rs (L190-192)
```rust
    pub fn transaction_fee_e8s_or_panic(&self) -> u64 {
        self.transaction_fee_e8s.unwrap()
    }
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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L244-261)
```rust
/// canister creation, and cannot be modified afterwards.
///
/// If the initialization parameters are incorrect, the swap will
/// immediately be aborted.
#[derive(
    candid::CandidType,
    candid::Deserialize,
    serde::Serialize,
    comparable::Comparable,
    Clone,
    PartialEq,
    ::prost::Message,
)]
pub struct Init {
    /// The canister ID of the NNS governance canister. This is the only
    /// principal that can open the swap.
    #[prost(string, tag = "1")]
    pub nns_governance_canister_id: ::prost::alloc::string::String,
```

**File:** rs/sns/init/src/lib.rs (L1003-1008)
```rust
    fn validate_transaction_fee_e8s(&self) -> Result<(), String> {
        match self.transaction_fee_e8s {
            Some(_) => Ok(()),
            None => Err("Error: transaction_fee_e8s must be specified.".to_string()),
        }
    }
```

**File:** rs/sns/swap/src/swap.rs (L401-404)
```rust
    pub fn new(init: Init) -> Self {
        if let Err(e) = init.validate() {
            panic!("Invalid init arg, reason: {e}\nArg: {init:#?}\n");
        }
```
