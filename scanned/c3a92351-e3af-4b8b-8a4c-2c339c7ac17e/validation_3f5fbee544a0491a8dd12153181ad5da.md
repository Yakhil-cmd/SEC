### Title
Lack of Incentivization for `finalize_swap` Creates Free-Rider Problem and Centralized Settlement Dependency - (File: rs/sns/swap/canister/canister.rs)

### Summary
The SNS Swap canister's `finalize_swap` endpoint is permissionless but provides no reward to callers. Auto-finalization is attempted exactly once via the heartbeat; if it fails, no automatic retry occurs and no participant has a protocol-enforced incentive to trigger manual finalization. This creates a free-rider problem structurally identical to the Divergence Protocol's `settle()` issue: all participants rationally wait for someone else to pay the cycles cost, potentially leaving ICP and SNS tokens locked indefinitely and forcing centralized intervention by DFINITY.

### Finding Description

`finalize_swap` in the SNS Swap canister is exposed as an unrestricted `#[update]` endpoint:

```rust
// rs/sns/swap/canister/canister.rs:150-158
#[update]
async fn finalize_swap(_arg: FinalizeSwapRequest) -> FinalizeSwapResponse {
    log!(INFO, "finalize_swap");
    let mut clients = swap()
        .init_or_panic()
        .environment()
        .expect("unable to create canister clients");
    swap_mut().finalize(now_fn, &mut clients).await
}
```

There is no caller check, no fee, and no reward. [1](#0-0) 

The canister's heartbeat (`run_periodic_tasks`) attempts auto-finalization via `try_auto_finalize`, but the critical design decision is that `already_tried_to_auto_finalize` is set to `Some(true)` **before** the finalization attempt completes, and the flag is never reset on failure:

```rust
// rs/sns/swap/src/swap.rs:719
self.already_tried_to_auto_finalize = Some(true);
// Attempt finalization
let auto_finalize_swap_response = self.finalize(now_fn, environment).await;
``` [2](#0-1) 

`can_auto_finalize()` then permanently blocks further automatic attempts:

```rust
// rs/sns/swap/src/swap.rs:2936-2940
if self.already_tried_to_auto_finalize.unwrap_or(true) {
    return Err(format!(
        "self.already_tried_to_auto_finalize is {:?}, indicating that an attempt has already been made to auto-finalize. No further attempts will be made automatically. Manually calling finalize is still allowed.",
        ...
    ));
}
``` [3](#0-2) 

The heartbeat path confirms this: once `can_auto_finalize()` returns `Err`, the `else if` branch is never entered again: [4](#0-3) 

Additionally, `should_auto_finalize` in `Init` can be set to `false`, disabling auto-finalization entirely from the start, making manual `finalize_swap` the only path: [5](#0-4) 

The `finalize_inner` pipeline makes multiple sequential inter-canister calls (`sweep_icp` → `settle_neurons_fund_participation` → `create_sns_neuron_recipes` → `sweep_sns` → `claim_swap_neurons` → `set_mode`), any one of which can fail transiently. A failure at any step halts the entire pipeline and returns an error, but `already_tried_to_auto_finalize` remains `true`. [6](#0-5) 

### Impact Explanation

When auto-finalization fails (or is disabled), the swap enters a state where:

- **COMMITTED swaps**: SNS tokens remain in the swap canister's ledger subaccounts; participants cannot claim their neurons. The SNS governance canister remains in `PreInitializationSwap` mode, blocking all governance actions.
- **ABORTED swaps**: Participants' ICP remains locked in the swap canister; `error_refund_icp` is the only recovery path but also has no incentive.

No protocol mechanism rewards a third party for calling `finalize_swap`. Every participant rationally prefers to free-ride on another participant's cycles expenditure. If all participants defect, funds remain locked indefinitely. The only recourse is centralized intervention by DFINITY or the SNS team, replicating the exact centralization risk described in the reference report.

### Likelihood Explanation

The `finalize_inner` pipeline makes at least 4–6 sequential inter-canister calls. Transient replica errors, canister-busy responses, or temporary unavailability of the ICP ledger or NNS governance canister during the single auto-finalization window are realistic failure modes. The `should_auto_finalize = false` configuration path also exists in production SNS deployments. The free-rider dynamic is a well-established coordination failure in permissionless systems with no reward.

### Recommendation

1. **Retry auto-finalization**: Reset `already_tried_to_auto_finalize` to `false` on a failed finalization attempt (one that returns `has_error_message() == true`), allowing the heartbeat to retry with backoff.
2. **Caller incentive**: Allocate a small fixed ICP reward (e.g., from swap proceeds or a protocol treasury) to the first caller who successfully triggers `finalize_swap` after the swap ends.
3. **Spam protection**: Combine incentivization with a minimum delay after swap termination before the reward is claimable, preventing front-running of the auto-finalization attempt.

### Proof of Concept

1. An SNS swap reaches `Lifecycle::Committed` with 100 participants and significant ICP locked.
2. The heartbeat fires and calls `try_auto_finalize`. The call to `settle_neurons_fund_participation` (which calls NNS governance) returns a transient `CanisterCallError`.
3. `already_tried_to_auto_finalize` is now `Some(true)`. The heartbeat will never attempt auto-finalization again.
4. `finalize_swap` is callable by any of the 100 participants, but each one pays ~0.0001 ICP in cycles and receives nothing in return.
5. Each participant waits for another to act. No one calls it.
6. Participants' SNS tokens remain unclaimed; SNS governance stays locked in `PreInitializationSwap` mode.
7. DFINITY must monitor the swap canister and manually submit `finalize_swap` — a centralized, off-chain dependency for a supposedly decentralized protocol. [1](#0-0) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L149-159)
```rust
/// See Swap.finalize.
#[update]
async fn finalize_swap(_arg: FinalizeSwapRequest) -> FinalizeSwapResponse {
    log!(INFO, "finalize_swap");
    let mut clients = swap()
        .init_or_panic()
        .environment()
        .expect("unable to create canister clients");

    swap_mut().finalize(now_fn, &mut clients).await
}
```

**File:** rs/sns/swap/src/swap.rs (L695-736)
```rust
    /// Attempts to finalize the swap. If this function calls [`Self::finalize`],
    /// it will set `self.already_tried_to_auto_finalize` to `Some(true)`, and
    /// won't try to finalize the swap again, even if called again.
    ///
    /// The argument 'now_fn' is a function that returns the current time
    /// for bookkeeping of transfers. For easier testing, it is given
    /// an argument that is 'false' to get the timestamp when a
    /// transfer is initiated and 'true' to get the timestamp when a
    /// transfer is successful.
    pub async fn try_auto_finalize(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> Result<FinalizeSwapResponse, String> {
        self.can_auto_finalize()?;

        // We don't want to try to finalize the swap more than once. So we'll
        // set `self.already_tried_to_auto_finalize` to true, so we don't try
        // again.
        log!(
            INFO,
            "Attempting to automatically finalize the swap at timestamp {}. (Will not automatically attempt again even if this fails.)",
            now_fn(false)
        );
        self.already_tried_to_auto_finalize = Some(true);

        // Attempt finalization
        let auto_finalize_swap_response = self.finalize(now_fn, environment).await;

        // Record the result
        if self.auto_finalize_swap_response.is_some() {
            log!(
                ERROR,
                "Somehow, auto-finalization happened twice (second time at {}). Overriding self.auto_finalize_swap_response, old value was: {:?}",
                now_fn(true),
                auto_finalize_swap_response,
            );
        }
        self.auto_finalize_swap_response = Some(auto_finalize_swap_response.clone());

        Ok(auto_finalize_swap_response)
    }
```

**File:** rs/sns/swap/src/swap.rs (L1055-1101)
```rust
        // Auto-finalize the swap
        // We discard the error, if there is one, because to log it would mean it would be logged
        // every time a periodic task is executed where we fall through to this point (and we don't
        // want to spam the logs).
        else if self.can_auto_finalize().is_ok() {
            // First, record when the finalization started, in case this function is
            // refactored to `await` before this point.
            let auto_finalization_start_seconds = now_fn(false);

            // Then, get the environment
            let environment = self
                .init
                .as_ref()
                .ok_or_else(|| "couldn't get `init`".to_string())
                .and_then(|init| init.environment());

            match environment {
                Err(error) => {
                    log!(
                        ERROR,
                        "Failed to get environment when attempting auto-finalization. Error: {error}"
                    );
                }
                Ok(mut environment) => {
                    // Then, attempt the auto-finalization
                    // `try_auto_finalize` will never return `Error` here
                    // because we already checked `self.can_auto_finalize()`
                    // above, and `try_auto_finalize` will only return an error
                    // if `can_auto_finalize` does.
                    // The FinalizeSwapResponse from finalization will be logged
                    // by `Self::finalize`.
                    if self
                        .try_auto_finalize(now_fn, &mut environment)
                        .await
                        .is_ok()
                    {
                        // The current time is now probably different than the time when
                        // auto-finalization began, due to the `await`.
                        let auto_finalization_finish_seconds = now_fn(true);
                        log!(
                            INFO,
                            "Swap auto-finalization finished at timestamp {auto_finalization_finish_seconds} (started at timestamp {auto_finalization_start_seconds})"
                        );
                    }
                }
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1544-1624)
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

        // Create the SnsNeuronRecipes based on the contribution of direct and NF participants
        finalize_swap_response
            .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Transfer the SNS tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_sns_result(self.sweep_sns(now_fn, environment.sns_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Once SNS tokens have been distributed to the correct accounts, claim
        // them as neurons on behalf of the Swap participants.
        finalize_swap_response.set_claim_neuron_result(
            self.claim_swap_neurons(environment.sns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );

        // The following step is non-critical, so we'll do it after we set
        // governance to normal mode, but only if there were no errors.
        if !finalize_swap_response.has_error_message() {
            finalize_swap_response.set_set_dapp_controllers_result(
                self.take_sole_control_of_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );
        }

        finalize_swap_response
    }
```

**File:** rs/sns/swap/src/swap.rs (L2916-2944)
```rust
    /// Returns Ok(()) if the swap can auto-finalize, and Err(reason) otherwise
    pub fn can_auto_finalize(&self) -> Result<(), String> {
        // Being allowed to finalize is a precondition for being allowed
        // to auto-finalize.
        self.can_finalize()?;

        let Some(init) = self.init.as_ref() else {
            return Err("unable to access swap's init".to_string());
        };

        // Fail early if `self.init.should_auto_finalize` doesn't indicate that
        // auto-finalization is enabled.
        if !init.should_auto_finalize.unwrap_or_default() {
            return Err(format!(
                "init.should_auto_finalize is {:?}, not attempting auto-finalization.",
                init.should_auto_finalize
            ));
        }

        // Fail early if we've already tried to auto-finalize the swap.
        if self.already_tried_to_auto_finalize.unwrap_or(true) {
            return Err(format!(
                "self.already_tried_to_auto_finalize is {:?}, indicating that an attempt has already been made to auto-finalize. No further attempts will be made automatically. Manually calling finalize is still allowed.",
                self.already_tried_to_auto_finalize
            ));
        }

        Ok(())
    }
```
