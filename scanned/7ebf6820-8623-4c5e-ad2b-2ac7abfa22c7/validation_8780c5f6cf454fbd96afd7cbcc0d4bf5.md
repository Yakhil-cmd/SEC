### Title
Single-Shot Auto-Finalization With No Caller Incentive Leaves SNS Swap Permanently Stuck on Transient Failure - (File: rs/sns/swap/src/swap.rs)

---

### Summary

The SNS Swap canister's auto-finalization mechanism attempts finalization exactly once and permanently disables itself regardless of whether the attempt succeeded or failed. There is no economic incentive for any external party to manually call `finalize_swap` to retry. If the single auto-finalization attempt encounters a transient error, the swap is permanently stuck: buyers' ICP is locked, SNS tokens are undistributed, SNS governance remains in restricted mode, and Neurons' Fund maturity stays reserved indefinitely.

---

### Finding Description

The SNS Swap canister lifecycle ends in either `Committed` or `Aborted`. In both cases, `finalize_swap` must be called to distribute funds and complete the process. The design relies on a single auto-finalization attempt via the canister's periodic task.

**Step 1 – Auto-finalization is attempted exactly once.**

In `try_auto_finalize` (`rs/sns/swap/src/swap.rs`), the flag `already_tried_to_auto_finalize` is set to `true` unconditionally *before* the finalization attempt:

```rust
// (Will not automatically attempt again even if this fails.)
self.already_tried_to_auto_finalize = Some(true);

// Attempt finalization
let auto_finalize_swap_response = self.finalize(now_fn, environment).await;
``` [1](#0-0) 

**Step 2 – `can_auto_finalize` permanently rejects further attempts.**

Once the flag is set, `can_auto_finalize` returns `Err` for all future periodic task invocations:

```rust
if self.already_tried_to_auto_finalize.unwrap_or(true) {
    return Err(format!(
        "self.already_tried_to_auto_finalize is {:?}, indicating that an attempt has already been made to auto-finalize. No further attempts will be made automatically. ...",
        self.already_tried_to_auto_finalize
    ));
}
``` [2](#0-1) 

**Step 3 – `run_periodic_tasks` stops scheduling auto-finalization.**

The periodic task only calls `try_auto_finalize` when `can_auto_finalize().is_ok()`. After the first attempt, this condition is permanently false: [3](#0-2) 

**Step 4 – `finalize_swap` is permissionless but provides no reward.**

The public `finalize_swap` endpoint has no access control and no caller reward:

```rust
#[update]
async fn finalize_swap(_arg: FinalizeSwapRequest) -> FinalizeSwapResponse {
    log!(INFO, "finalize_swap");
    let mut clients = swap().init_or_panic().environment()
        .expect("unable to create canister clients");
    swap_mut().finalize(now_fn, &mut clients).await
}
``` [4](#0-3) 

**Step 5 – Finalization can fail mid-way on transient errors.**

`finalize_inner` is a multi-step pipeline that halts on any sub-step failure. For example, a transient error from NNS governance during `settle_neurons_fund_participation` halts the entire pipeline:

```rust
finalize_swap_response.set_settle_neurons_fund_participation_result(
    self.settle_neurons_fund_participation(environment.nns_governance_mut()).await,
);
if finalize_swap_response.has_error_message() {
    return finalize_swap_response;
}
``` [5](#0-4) 

The error message is explicit: `"Settling the Neurons' Fund participation did not succeed. Halting swap finalization"`. [6](#0-5) 

The protocol comment itself acknowledges the original design intent that `finalize` was not automatic precisely because errors need a caller to respond to:

> "The call to `finalize` does not happen automatically (i.e., on the canister heartbeat) so that there is a caller to respond to with potential errors." [7](#0-6) 

Yet the auto-finalization mechanism was added without providing any incentive for the fallback manual path.

---

### Impact Explanation

If the single auto-finalization attempt fails due to a transient error (NNS governance temporarily unavailable, ICP ledger error, replica rejection):

- **COMMITTED state:** All buyers' ICP is locked inside the Swap canister. SNS tokens are not distributed to participants. SNS governance remains in `PreInitializationSwap` restricted mode and cannot operate. Neurons' Fund maturity remains reserved and locked from NNS neurons indefinitely.
- **ABORTED state:** Buyers' ICP is not refunded. Dapp canister controllers are not restored to fallback principals, leaving dapp canisters under SNS Root control with no functioning SNS governance.

The `FinalizeSwapRequest` is an empty message — there is no fee, no reward, and no mechanism to compensate a third party for paying cycles to retry. For smaller or less-watched SNS projects, no party may ever call `finalize_swap` manually.

**Vulnerability class:** Ledger conservation bug / governance authorization bug — funds are permanently locked and governance is permanently restricted.

---

### Likelihood Explanation

**Low.** Auto-finalization is triggered by the canister's periodic timer. For it to fail, a transient error must occur during the single attempt window. Transient errors in inter-canister calls (NNS governance, ICP ledger) are uncommon but possible, particularly during subnet upgrades, high load, or canister restarts. The risk is elevated for SNS projects with no active technical team monitoring the swap canister's `auto_finalize_swap_response` field.

---

### Recommendation

1. **Retry auto-finalization on failure.** Instead of permanently setting `already_tried_to_auto_finalize = true` before the attempt, only set it after a *successful* finalization. Allow the periodic task to retry on failure up to a bounded number of times or until a timeout.

2. **Provide a caller incentive.** Allocate a small percentage of the swap proceeds (e.g., from the SNS treasury or a dedicated fee) as a reward to the first caller who successfully triggers `finalize_swap`, analogous to keeper incentives in DeFi protocols.

3. **Emit an observable alert.** If auto-finalization fails, emit a certified metric or log that monitoring infrastructure can detect, prompting the SNS team to intervene.

---

### Proof of Concept

1. An SNS decentralization swap reaches `Lifecycle::Committed` with 100 buyers and Neurons' Fund participation.
2. The canister's periodic task fires and calls `run_periodic_tasks` → `try_auto_finalize`.
3. `already_tried_to_auto_finalize` is set to `Some(true)` at line 719 of `swap.rs`.
4. `finalize_inner` calls `settle_neurons_fund_participation`, which makes an inter-canister call to NNS governance. NNS governance is temporarily unavailable (e.g., mid-upgrade) and returns a replica error.
5. `set_settle_neurons_fund_participation_result` sets `error_message` and `finalize_inner` returns early.
6. `auto_finalize_swap_response` is stored with an error message.
7. All subsequent periodic task invocations reach `can_auto_finalize()` → `Err("an attempt has already been made to auto-finalize")` and skip finalization.
8. `finalize_swap` is callable by anyone but provides no reward. No external party has economic incentive to pay cycles and call it.
9. Result: 100 buyers' ICP is permanently locked in the Swap canister. SNS governance remains in restricted mode. Neurons' Fund maturity is permanently reserved from NNS neurons. The SNS is effectively dead.

### Citations

**File:** rs/sns/swap/src/swap.rs (L714-722)
```rust
        log!(
            INFO,
            "Attempting to automatically finalize the swap at timestamp {}. (Will not automatically attempt again even if this fails.)",
            now_fn(false)
        );
        self.already_tried_to_auto_finalize = Some(true);

        // Attempt finalization
        let auto_finalize_swap_response = self.finalize(now_fn, environment).await;
```

**File:** rs/sns/swap/src/swap.rs (L1059-1101)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1563-1570)
```rust
        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L2935-2941)
```rust
        // Fail early if we've already tried to auto-finalize the swap.
        if self.already_tried_to_auto_finalize.unwrap_or(true) {
            return Err(format!(
                "self.already_tried_to_auto_finalize is {:?}, indicating that an attempt has already been made to auto-finalize. No further attempts will be made automatically. Manually calling finalize is still allowed.",
                self.already_tried_to_auto_finalize
            ));
        }
```

**File:** rs/sns/swap/canister/canister.rs (L150-159)
```rust
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

**File:** rs/sns/swap/src/types.rs (L955-961)
```rust
        if !settle_neurons_fund_participation_result.is_successful_settle() {
            self.set_error_message(
                "Settling the Neurons' Fund participation did not succeed. Halting swap finalization".to_string());
        }
        self.settle_neurons_fund_participation_result =
            Some(settle_neurons_fund_participation_result);
    }
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L136-140)
```text
// the swap. In this state, a call to `finalize` will create SNS
// neurons for each participant and transfer ICP to the SNS governance
// canister. The call to `finalize` does not happen automatically
// (i.e., on the canister heartbeat) so that there is a caller to
// respond to with potential errors.
```
