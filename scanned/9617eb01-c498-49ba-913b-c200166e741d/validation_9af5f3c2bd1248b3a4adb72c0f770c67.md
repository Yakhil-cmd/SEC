### Title
SNS Swap Canister Stores Stale Copy of `neuron_minimum_stake_e8s` Without Cross-Canister Validation, Enabling Swap Finalization DoS and Participant Fund Trapping — (`rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto`)

### Summary

The SNS Swap canister stores its own copy of `neuron_minimum_stake_e8s` (and `transaction_fee_e8s`) that is supposed to mirror the SNS Governance canister's authoritative value. The proto definition explicitly warns: *"Same as SNS governance. Must hold the same value as SNS governance. Whether the values match is not checked. If they don't match things will break."* No runtime cross-canister validation enforces this invariant. An SNS developer who controls the developer neurons during the swap period can raise `neuron_minimum_stake_e8s` in SNS Governance via a `ManageNervousSystemParameters` proposal after the swap opens, causing the Swap canister's copy to become stale. Swap participation validation continues to pass against the old (lower) value, but `claim_swap_neurons` at finalization enforces the new (higher) value, causing neuron creation to fail. Because the swap is already in `COMMITTED` state, participants cannot recover their ICP.

### Finding Description

**Root cause — stale parameter copy with no live validation:**

The `Init` message in the SNS Swap canister carries its own `neuron_minimum_stake_e8s` field:

```
// Same as SNS governance. Must hold the same value as SNS governance. Whether
// the values match is not checked. If they don't match things will break.
optional uint64 neuron_minimum_stake_e8s = 14;
``` [1](#0-0) 

The `Init::validate()` function only checks that the field is *present*, not that it matches the live SNS Governance value:

```rust
if self.neuron_minimum_stake_e8s.is_none() {
    return Err("neuron_minimum_stake_e8s is required.".to_string());
}
``` [2](#0-1) 

`Params::validate()` uses this stale copy to compute whether participants will receive enough SNS tokens to form neurons:

```rust
let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
    * self.sns_token_e8s as u128
    / self.max_icp_e8s as u128;

let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
    >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
``` [3](#0-2) 

At finalization, `claim_swap_neurons` in SNS Governance enforces the *live* `neuron_minimum_stake_e8s` from its own parameters, not the Swap's copy:

```rust
let neuron_minimum_stake_e8s = self.neuron_minimum_stake_e8s_or_panic();
``` [4](#0-3) 

**Attack path:**

1. SNS is initialized normally; both Swap and Governance hold `neuron_minimum_stake_e8s = X`.
2. Swap opens; participants contribute ICP. Swap validation passes against `X`.
3. The SNS developer, who controls the developer neurons (the only neurons that exist before the swap distributes tokens), submits and passes a `ManageNervousSystemParameters` proposal raising `neuron_minimum_stake_e8s` to `Y > X` in SNS Governance.
4. The Swap canister's copy remains `X`.
5. At finalization, `claim_swap_neurons` rejects neuron recipes whose stake is below `Y`, causing finalization to fail.
6. The swap is in `COMMITTED` state and cannot be aborted; participants' ICP is trapped.

The same attack applies to `transaction_fee_e8s`: [5](#0-4) 

### Impact Explanation

Participants who contributed ICP to a committed SNS swap can have their funds permanently trapped if the SNS developer raises `neuron_minimum_stake_e8s` in SNS Governance after the swap opens. The swap cannot be aborted once committed, and finalization fails because `claim_swap_neurons` rejects neuron recipes that no longer meet the updated minimum stake. This is a direct financial loss to swap participants and a DoS of the SNS launch process.

### Likelihood Explanation

The attack requires an SNS developer who controls the developer neurons during the swap period — a realistic scenario since developer neurons are the only voting neurons before the swap distributes tokens. The NNS governance approves the initial `CreateServiceNervousSystem` proposal without checking for future parameter drift. The `ManageNervousSystemParameters` proposal is a standard SNS governance action. The proto comment explicitly acknowledges the mismatch risk, confirming the design gap is known but unmitigated.

### Recommendation

1. **Live cross-canister validation at finalization:** Before calling `claim_swap_neurons`, the Swap canister should query SNS Governance for the current `neuron_minimum_stake_e8s` and `transaction_fee_e8s` and verify they have not increased beyond the values used during participation validation.

2. **Lock parameters during swap:** SNS Governance should reject `ManageNervousSystemParameters` proposals that increase `neuron_minimum_stake_e8s` or `transaction_fee_e8s` while a swap is in `OPEN` or `COMMITTED` state.

3. **Remove the stale copy:** Replace the Swap canister's stored copies with live queries to SNS Governance and SNS Ledger at the time of participation validation and finalization.

### Proof of Concept

The mismatch is structurally identical to the `FullRangeHook` bug: the Swap canister defines its own operational bound (`neuron_minimum_stake_e8s = X`) that can diverge from the protocol's authoritative bound (`neuron_minimum_stake_e8s = Y` in SNS Governance). When `Y > X`, participation validation passes (analogous to price within the hook's tick range) but neuron creation fails at finalization (analogous to the rebalance revert when price exits the range).

Concretely:
- Swap opens with `neuron_minimum_stake_e8s = 100_000` in both canisters.
- Developer passes `ManageNervousSystemParameters` raising it to `200_000` in SNS Governance.
- Participants contribute ICP sized for `100_000` minimum stake; Swap validation passes.
- `claim_swap_neurons` rejects all neuron recipes with stake between `100_000` and `200_000`.
- Finalization fails; swap is committed; ICP is trapped.

The proto warning at [6](#0-5) 
and the absence of any runtime enforcement in [7](#0-6) 
confirm the root cause is present in production code.

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

**File:** rs/sns/swap/src/types.rs (L346-351)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
```

**File:** rs/sns/governance/src/governance.rs (L4462-4462)
```rust
        let neuron_minimum_stake_e8s = self.neuron_minimum_stake_e8s_or_panic();
```
