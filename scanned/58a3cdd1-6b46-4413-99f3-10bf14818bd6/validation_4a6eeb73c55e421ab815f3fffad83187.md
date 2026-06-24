### Title
Missing Embedded Incentives to Complete SNS Swap Finalization — (`rs/sns/swap/src/swap.rs`, `rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS Swap canister requires an external caller to invoke `finalize_swap` to complete the decentralization swap process. When `should_auto_finalize` is `false`, finalization is entirely manual. When `should_auto_finalize` is `true`, auto-finalization is attempted exactly once via the heartbeat; if that single attempt fails, the flag `already_tried_to_auto_finalize` is permanently set to `true` and no further automatic attempts are made. In both cases, there are no embedded on-chain incentives for any party to call `finalize_swap` manually, leaving participants' ICP tokens locked in the swap canister indefinitely.

---

### Finding Description

The SNS Swap canister's `finalize_swap` endpoint is permissionless — any principal can call it — but there is no protocol-level incentive to do so.

**Path 1 — `should_auto_finalize = false`:**
The proto field `should_auto_finalize` controls whether the heartbeat will attempt finalization. When set to `false`, `finalize_swap` must be called manually with no automatic fallback. [1](#0-0) 

**Path 2 — Auto-finalization fails and is never retried:**
In `try_auto_finalize`, the flag `already_tried_to_auto_finalize` is set to `Some(true)` *before* the finalization attempt. If `finalize` returns an error (e.g., transient ICP ledger failure, NNS governance unavailability), the flag remains `true` and `can_auto_finalize` will permanently reject all future heartbeat-triggered attempts. [2](#0-1) [3](#0-2) 

The log message itself acknowledges this is by design: *"Will not automatically attempt again even if this fails."* [4](#0-3) 

The `finalize_swap` canister endpoint is open to any caller with no access control: [5](#0-4) 

The proto documentation also explicitly acknowledges the manual dependency: *"The call to `finalize` does not happen automatically (i.e., on the canister heartbeat) so that there is a caller to respond to with potential errors."* [6](#0-5) 

**What `finalize_inner` does and why it matters:**
`finalize_inner` performs: `sweep_icp` (return ICP to buyers on abort, or send to SNS treasury on commit), `settle_neurons_fund_participation`, `sweep_sns` (distribute SNS tokens), `claim_swap_neurons`, and `set_sns_governance_to_normal_mode`. If none of these run, participants' ICP is locked and SNS governance remains in `PreInitializationSwap` mode. [7](#0-6) 

---

### Impact Explanation

If `finalize_swap` is never called (or auto-finalization fails and is not retried):

1. **Committed swap:** Participants' ICP tokens remain locked in the swap canister. SNS tokens are not distributed. SNS neurons are not created. SNS governance stays in `PreInitializationSwap` mode, blocking all governance actions.
2. **Aborted swap:** Participants cannot recover their ICP tokens because `sweep_icp` (which refunds buyers) is only executed inside `finalize`.
3. **Neurons' Fund:** Reserved maturity is not refunded to NNS neurons, causing permanent maturity loss for NNS neuron holders.

The impact is a permanent locking of user funds and a non-functional SNS, with no protocol-enforced recovery path.

---

### Likelihood Explanation

- Transient failures in the ICP ledger or NNS governance canister during the single auto-finalization attempt are realistic (e.g., canister busy, inter-canister call timeout, subnet under load).
- When `should_auto_finalize = false`, the entire finalization depends on an external actor with no on-chain incentive.
- Participants have an *intrinsic* incentive to recover their funds, but they must: (a) notice the failure, (b) know to call `finalize_swap` manually, and (c) pay cycles for the call. There is no protocol-level notification, no gas station, and no reward for doing so.
- The system currently depends on the SNS team or participants acting as a centralized off-chain coordinator — exactly the pattern flagged in the external report.

---

### Recommendation

1. **Retry on failure:** Remove the "attempt only once" restriction. Instead of permanently setting `already_tried_to_auto_finalize = Some(true)` before the attempt, only set it after a *successful* finalization. Allow the heartbeat to retry on failure (with a backoff).
2. **Incentivize manual callers:** Allocate a small portion of the swap proceeds (e.g., a fixed ICP fee) as a reward to the first caller who successfully triggers `finalize_swap`, paid out during `sweep_icp`.
3. **Document the dependency:** At minimum, emit a canister log or expose a query endpoint that clearly signals to participants that manual finalization is required, so they know to act.

---

### Proof of Concept

1. Deploy an SNS swap with `should_auto_finalize = true`.
2. Arrange for the ICP ledger to return a transient error during the heartbeat-triggered `finalize` call (e.g., by temporarily making the ledger canister busy).
3. Observe that `already_tried_to_auto_finalize` is set to `Some(true)` and `auto_finalize_swap_response` contains an error.
4. Observe that all subsequent heartbeats skip finalization because `can_auto_finalize()` returns `Err(...)`.
5. Participants' ICP is now locked. No automatic recovery occurs. Manual `finalize_swap` calls succeed, but there is no on-chain mechanism to notify or incentivize anyone to make them. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L136-140)
```text
// the swap. In this state, a call to `finalize` will create SNS
// neurons for each participant and transfer ICP to the SNS governance
// canister. The call to `finalize` does not happen automatically
// (i.e., on the canister heartbeat) so that there is a caller to
// respond to with potential errors.
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L422-426)
```text
  // Controls whether swap finalization should be attempted automatically in the
  // canister heartbeat. If set to false, `finalize_swap` must be called
  // manually. Note: it is safe to call `finalize_swap` multiple times
  // (regardless of the value of this field).
  optional bool should_auto_finalize = 28;
```

**File:** rs/sns/swap/src/swap.rs (L695-735)
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
```

**File:** rs/sns/swap/src/swap.rs (L1014-1101)
```rust
    pub async fn run_periodic_tasks(&mut self, now_fn: fn(bool) -> u64) {
        let periodic_task_start_seconds = now_fn(false);

        // Purge old tickets
        const NUMBER_OF_TICKETS_THRESHOLD: u64 = 100_000_000; // 100M * ~size(ticket) = ~25GB
        const TWO_DAYS_IN_NANOSECONDS: u64 = 60 * 60 * 24 * 2 * 1_000_000_000;
        const MAX_NUMBER_OF_PRINCIPALS_TO_INSPECT: u64 = 100_000;

        self.try_purge_old_tickets(
            ic_cdk::api::time,
            NUMBER_OF_TICKETS_THRESHOLD,
            TWO_DAYS_IN_NANOSECONDS,
            MAX_NUMBER_OF_PRINCIPALS_TO_INSPECT,
        );

        // Automatically transition the state. Only one state transition per periodic task.

        // Auto-open the swap
        if self.try_open(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap opened at timestamp {}",
                periodic_task_start_seconds
            );
        }
        // Auto-commit the swap
        else if self.try_commit(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap committed at timestamp {}",
                periodic_task_start_seconds
            );
        }
        // Auto-abort the swap
        else if self.try_abort(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap aborted at timestamp {}",
                periodic_task_start_seconds
            );
        }
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

**File:** rs/sns/swap/src/swap.rs (L1544-1612)
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
