### Title
No Mechanism to Change `fallback_controller_principal_ids` in SNS Swap Canister - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `Init` struct, which holds `fallback_controller_principal_ids`, is explicitly documented as immutable after canister creation. If the swap is aborted, these principals receive sole control of all dapp canisters. There is no on-chain mechanism for SNS governance, NNS governance, or any other party to update these principals during the swap lifecycle, mirroring the KintoWallet recoverer-lock bug.

---

### Finding Description

The SNS Swap canister stores its initialization parameters in an `Init` struct. The proto definition and generated Rust code both carry an explicit comment:

> "The initialisation data of the canister. Always specified on canister creation, and **cannot be modified afterwards**." [1](#0-0) [2](#0-1) 

Within this immutable `Init`, the field `fallback_controller_principal_ids` holds the principals that receive control of all dapp canisters if the swap is aborted: [3](#0-2) [4](#0-3) 

When the swap lifecycle ends in `Aborted` state, `restore_dapp_controllers` is called, which reads `fallback_controller_principal_ids` directly from the frozen `Init` and calls `sns_root.set_dapp_controllers` with those principals: [5](#0-4) 

The validation in `types.rs` enforces that at least one fallback controller must be present, but provides no mechanism to update them post-deployment: [6](#0-5) 

There is no `update_fallback_controllers` endpoint, no governance proposal type targeting this field, and no `post_upgrade` path that re-reads or allows overriding `fallback_controller_principal_ids` from stable memory. The field is frozen for the entire swap duration (which can span weeks).

---

### Impact Explanation

If the swap is aborted (e.g., minimum participation not reached), `restore_dapp_controllers` unconditionally hands sole control of all registered dapp canisters to the immutable `fallback_controller_principal_ids`. If those principals are compromised or unavailable at abort time, the dapp canisters — and all their state and cycles — are permanently transferred to the wrong party with no on-chain recovery path. Neither SNS governance (which is in `PreInitializationSwap` mode and has restricted proposal types) nor NNS governance has a direct mechanism to override the frozen `fallback_controller_principal_ids` before finalization executes. [7](#0-6) 

---

### Likelihood Explanation

SNS swaps run for days to weeks. The `fallback_controller_principal_ids` are set at SNS creation time via an NNS `CreateServiceNervousSystem` proposal. During the swap window, the developers' key material may be rotated, lost, or compromised. Because `finalize_swap` is callable by anyone once the swap lifecycle ends, an external party can trigger the irreversible `restore_dapp_controllers` call at any time after abort, locking in the stale or compromised principals. The combination of a long swap window, permissionless finalization, and a frozen privileged-role list makes this a realistic operational risk for any SNS launch. [8](#0-7) 

---

### Recommendation

Add a governance-gated mechanism — callable by NNS governance or the SNS governance canister (while in `PreInitializationSwap` mode) — to update `fallback_controller_principal_ids` before the swap is finalized. Concretely:

1. Move `fallback_controller_principal_ids` out of the frozen `Init` struct into a mutable field of the `Swap` state.
2. Expose an `update_fallback_controllers` endpoint restricted to the `nns_governance_canister_id` (already stored in `Init`).
3. Validate the new list with the same rules as `Init::validate` enforces today. [9](#0-8) 

---

### Proof of Concept

1. NNS adopts a `CreateServiceNervousSystem` proposal; the SNS Swap canister is deployed with `fallback_controller_principal_ids = [developer_principal]`.
2. The swap opens and runs for its full duration but fails to reach `min_participants`.
3. The swap lifecycle transitions to `Aborted`.
4. At any point after abort, an unprivileged caller sends an ingress `finalize_swap {}` message to the Swap canister.
5. `finalize_inner` calls `restore_dapp_controllers`, which reads the frozen `Init.fallback_controller_principal_ids` and calls `sns_root.set_dapp_controllers` with `[developer_principal]`.
6. If `developer_principal`'s key was rotated or compromised between step 1 and step 5, the dapp canisters are now permanently under the wrong controller with no on-chain remedy. [10](#0-9) [5](#0-4)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L283-288)
```text
// The initialisation data of the canister. Always specified on
// canister creation, and cannot be modified afterwards.
//
// If the initialization parameters are incorrect, the swap will
// immediately be aborted.
message Init {
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L310-313)
```text
  // If the swap is aborted, control of the canister(s) should be set to these
  // principals. Must not be empty.
  repeated string fallback_controller_principal_ids = 11;

```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L244-248)
```rust
/// canister creation, and cannot be modified afterwards.
///
/// If the initialization parameters are incorrect, the swap will
/// immediately be aborted.
#[derive(
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L278-282)
```rust
    /// If the swap is aborted, control of the canister(s) should be set to these
    /// principals. Must not be empty.
    #[prost(string, repeated, tag = "11")]
    pub fallback_controller_principal_ids: ::prost::alloc::vec::Vec<::prost::alloc::string::String>,
    /// Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
```

**File:** rs/sns/swap/src/swap.rs (L1344-1350)
```rust

    /// Determines if the conditions have been met in order to
    /// restore the dapp canisters to the fallback controller ids.
    /// The lifecycle MUST be set to Aborted via the commit method.
    pub fn should_restore_dapp_control(&self) -> bool {
        self.lifecycle() == Lifecycle::Aborted
    }
```

**File:** rs/sns/swap/src/swap.rs (L1354-1381)
```rust
    pub async fn restore_dapp_controllers(
        &self,
        sns_root_client: &mut impl SnsRootClient,
    ) -> Result<Result<SetDappControllersResponse, CanisterCallError>, String> {
        let (controller_principal_ids, errors): (Vec<PrincipalId>, Vec<String>) = self
            .init()?
            .fallback_controller_principal_ids
            .iter()
            .map(|maybe_principal_id| PrincipalId::from_str(maybe_principal_id))
            .partition_map(|result| match result {
                Ok(p) => Either::Left(p),
                Err(msg) => Either::Right(msg.to_string()),
            });

        if !errors.is_empty() {
            return Err(format!(
                "Could not set_dapp_controllers, one or more fallback_controller_principal_ids \
                could not be parsed as a PrincipalId. {:?}",
                errors.join("\n")
            ));
        }

        Ok(sns_root_client
            .set_dapp_controllers(SetDappControllersRequest {
                canister_ids: None,
                controller_principal_ids,
            })
            .await)
```

**File:** rs/sns/swap/src/swap.rs (L1544-1584)
```rust
    pub async fn finalize_inner(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        let mut finalize_swap_response = FinalizeSwapResponse::default();

        if let Err(e) = self.can_finalize() {
            finalize_swap_response.set_error_message(e);
            return finalize_swap_response;
        }

        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
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
