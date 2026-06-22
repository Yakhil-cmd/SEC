### Title
Unchecked Assumption That Swap Canister's Cached `transaction_fee_e8s` Matches SNS Ledger Fee — (File: `rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto`)

---

### Summary

The SNS Swap canister stores `transaction_fee_e8s` and `neuron_minimum_stake_e8s` locally in its `Init` struct and uses them during swap finalization. These values are assumed to match the actual values in the SNS Ledger and SNS Governance canisters respectively, but this assumption is **never verified on-chain**. The code itself explicitly acknowledges this: *"Whether the values match is not checked. If they don't match things will break."*

---

### Finding Description

In `rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto`, the `Init` message carries two locally-cached values: [1](#0-0) 

The validation in `rs/sns/swap/src/types.rs` only checks that these fields are *present*, not that they match the actual canister values: [2](#0-1) 

During `sweep_sns`, the locally cached fee is used directly to compute transfer amounts for every SNS token distribution: [3](#0-2) 

The SNS token transfer destination is computed using `sns_governance` as the account owner, with the locally cached fee deducted: [4](#0-3) 

Similarly, `Params::validate` uses the cached `neuron_minimum_stake_e8s` to gate participation, without querying SNS Governance: [5](#0-4) 

The SNS Ledger's `transaction_fee` is independently configurable and can be changed post-initialization via `UpgradeArgs`: [6](#0-5) 

The SNS Governance's `neuron_minimum_stake_e8s` is independently configurable via `ManageNervousSystemParameters` proposals: [7](#0-6) 

---

### Impact Explanation

**Scenario A — `init.transaction_fee_e8s` < actual SNS ledger fee:**
`sweep_sns` submits transfers with an insufficient fee. The SNS ledger rejects every transfer with `BadFee`. SNS tokens cannot be distributed to any swap participant. The swap is permanently stuck in a broken finalization state.

**Scenario B — `init.transaction_fee_e8s` > actual SNS ledger fee:**
`sweep_sns` over-deducts fees from each participant's allocation. Every participant receives fewer SNS tokens than they are entitled to. This is a ledger conservation bug — tokens are silently destroyed beyond what the ledger actually charges.

**Scenario C — `init.neuron_minimum_stake_e8s` < actual SNS governance minimum:**
`Params::validate` passes, allowing participation amounts that produce per-neuron stakes below the actual governance minimum. `sweep_sns` transfers SNS tokens to the neuron staking subaccounts. `claim_swap_neurons` then fails for those neurons because SNS Governance rejects claims below its actual minimum stake. The SNS tokens are permanently stranded in the staking subaccounts with no recovery path.

---

### Likelihood Explanation

The divergence can arise through two realistic paths:

1. **Post-initialization governance proposal**: An SNS governance proposal (executable by any governance majority, including a legitimate one) changes the SNS ledger's `transaction_fee` or SNS governance's `neuron_minimum_stake_e8s` after the swap `Init` is fixed. The Swap canister has no mechanism to detect or react to this change.

2. **Initialization misconfiguration**: SNS-W sets `transaction_fee_e8s` in the Swap `Init` to a value that does not match the SNS ledger's actual fee at deployment time. Since no on-chain cross-canister verification is performed, this divergence is silent until finalization.

The code comment *"Whether the values match is not checked. If they don't match things will break"* is an explicit acknowledgment that this precondition is unenforced. The SNS swap is a one-time, irreversible event; any divergence discovered at finalization time has no remediation path short of a canister upgrade.

---

### Recommendation

During `sweep_sns` (or at `open` time), the Swap canister should query the SNS Ledger for its current `icrc1_fee` and the SNS Governance for its current `neuron_minimum_stake_e8s`, and assert that these match the cached `Init` values before proceeding. If they do not match, the operation should halt with a clear error rather than silently using stale values. Alternatively, the Swap canister should always read the fee directly from the SNS Ledger at transfer time rather than caching it at initialization.

---

### Proof of Concept

1. Deploy an SNS with `transaction_fee_e8s = 10_000` in both the SNS Ledger and the Swap `Init`.
2. After the swap opens, pass an SNS governance proposal (`UpgradeArgs { change_fee_collector: ..., transfer_fee: Some(20_000) }`) to raise the SNS ledger fee to `20_000`.
3. Allow the swap to reach the `Committed` state and call `finalize_swap`.
4. `sweep_sns` calls `transfer_funds` with `fee_e8s = 10_000` (the cached value).
5. The SNS ledger rejects every transfer with `BadFee { expected_fee: 20_000 }`.
6. `sweep_sns` returns a `SweepResult` with all entries in `failure`, and SNS tokens are never distributed to participants.

The root cause — the Swap canister using its own locally stored `transaction_fee_e8s` instead of querying `sns_ledger.icrc1_fee()` — is directly analogous to the external report's `reclaimTokens` using `owner` (trustee's owner) instead of `tokenContract.owner`. [8](#0-7)

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

**File:** rs/sns/swap/src/types.rs (L296-307)
```rust
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
```

**File:** rs/sns/swap/src/types.rs (L332-367)
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

        if !min_participant_icp_e8s_big_enough {
            return Err(format!(
                "min_participant_icp_e8s={} is too small. It needs to be \
                 large enough to ensure that participants will end up with \
                 enough SNS tokens to form {} SNS neurons, each of which \
                 require at least {} SNS e8s, plus {} e8s in transaction \
                 fees. More precisely, the following inequality must hold: \
                 min_participant_icp_e8s >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) * max_icp_e8s / sns_token_e8s \
                 (where / denotes floor division).",
                self.min_participant_icp_e8s,
                neuron_basket_count,
                neuron_minimum_stake_e8s,
                transaction_fee_e8s,
            ));
        }
```

**File:** rs/sns/swap/src/swap.rs (L2165-2197)
```rust
    pub async fn sweep_sns(
        &mut self,
        now_fn: fn(bool) -> u64,
        sns_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting sweep_sns(). SNS Tokens cannot be distributed if \
                Lifecycle is not COMMITTED. Current Lifecycle: {:?}",
                self.lifecycle()
            );
            return SweepResult::new_with_global_failures(1);
        }

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_sns(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let sns_governance = init.sns_governance_or_panic();
        let nns_governance = init.nns_governance_or_panic();
        let sns_transaction_fee_tokens = Tokens::from_e8s(init.transaction_fee_e8s_or_panic());

        let mut sweep_result = SweepResult::default();
```

**File:** rs/sns/swap/src/swap.rs (L2244-2248)
```rust
            };
            let dst = Account {
                owner: sns_governance.get().0,
                subaccount: Some(dst_subaccount),
            };
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L943-950)
```rust
        if let Some(change_fee_collector) = args.change_fee_collector {
            self.fee_collector = change_fee_collector.into();
            if self.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(self.minting_account)
            {
                ic_cdk::trap(
                    "The fee collector account cannot be the same account as the minting account",
                );
            }
```

**File:** rs/sns/governance/src/governance.rs (L3375-3380)
```rust
    /// Returns the neuron minimum stake e8s from the nervous system parameters.
    fn neuron_minimum_stake_e8s_or_panic(&self) -> u64 {
        self.nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s")
    }
```
