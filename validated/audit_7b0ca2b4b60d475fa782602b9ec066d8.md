### Title
One-Shot Auto-Finalization Flag Set Before Success Permanently Disables Automatic Fund Distribution on Transient Failure - (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS Swap canister's `try_auto_finalize` function sets `already_tried_to_auto_finalize = true` **before** the finalization attempt completes. If `finalize` fails due to a transient error (e.g., a temporary ICP ledger unavailability during `sweep_icp`), the flag is permanently set, the periodic task timer is disabled, and all ICP/SNS tokens held in the swap canister remain undistributed indefinitely with no automatic recovery path.

### Finding Description

In `rs/sns/swap/src/swap.rs`, `try_auto_finalize` unconditionally sets the one-shot guard before calling `finalize`:

```rust
// Line 719 — flag set BEFORE the attempt
self.already_tried_to_auto_finalize = Some(true);

// Line 722 — finalization attempt (may fail)
let auto_finalize_swap_response = self.finalize(now_fn, environment).await;
``` [1](#0-0) 

`can_auto_finalize` then permanently blocks any further automatic attempt once the flag is `true`:

```rust
if self.already_tried_to_auto_finalize.unwrap_or(true) {
    return Err(format!(
        "... No further attempts will be made automatically. Manually calling finalize is still allowed.",
        ...
    ));
}
``` [2](#0-1) 

`requires_periodic_tasks` returns `false` once the flag is set and the lifecycle is terminal, causing the canister timer to be disabled:

```rust
pub fn requires_periodic_tasks(&self) -> bool {
    !self.lifecycle_is_terminal() || !self.already_tried_to_auto_finalize.unwrap_or(true)
}
``` [3](#0-2) 

`run_periodic_tasks` only attempts auto-finalization when `can_auto_finalize().is_ok()`, which is permanently false after the first attempt: [4](#0-3) 

`finalize_inner` halts on the first sub-step failure (e.g., `sweep_icp` returning a non-successful `SweepResult`), leaving buyer ICP subaccounts untouched: [5](#0-4) 

The `set_sweep_icp_result` helper marks the response as errored on any partial failure: [6](#0-5) 

### Impact Explanation

When a swap reaches `COMMITTED` or `ABORTED` and auto-finalization fires but encounters a transient ICP ledger error:

- All buyer ICP deposits held in per-buyer subaccounts of the swap canister remain locked.
- In the `ABORTED` case, participants cannot receive their ICP refunds automatically.
- In the `COMMITTED` case, ICP is not forwarded to SNS governance and SNS tokens are not distributed.
- The periodic task timer is permanently disabled (`requires_periodic_tasks` → `false`).
- No on-chain mechanism retries the distribution; manual intervention via `finalize_swap` is required but not guaranteed to happen.

The `BuyerState` proto confirms ICP is held in escrow until transfer completes: [7](#0-6) 

### Likelihood Explanation

The ICP ledger is a live canister subject to transient unavailability during upgrades or high load. Auto-finalization fires exactly once, immediately after the swap reaches a terminal state. The window for a transient ledger failure to coincide with this single attempt is realistic and has precedent (the ckBTC minter has experienced analogous stuck-withdrawal incidents requiring emergency upgrades). [8](#0-7) 

No privileged access is required; the failure is triggered by normal canister lifecycle progression.

### Recommendation

Set `already_tried_to_auto_finalize = Some(true)` only **after** a fully successful finalization (i.e., after `finalize` returns a response with no `error_message`). If finalization fails, leave the flag as `false` so the periodic task retries on the next heartbeat. Alternatively, implement a bounded retry counter with exponential backoff rather than a permanent one-shot flag.

### Proof of Concept

1. A SNS swap reaches `COMMITTED` state with buyers holding ICP in subaccounts.
2. The periodic task fires and calls `try_auto_finalize`.
3. At line 719, `already_tried_to_auto_finalize` is set to `true`.
4. `finalize` → `finalize_inner` → `sweep_icp` is called; the ICP ledger returns a transient error for one buyer.
5. `set_sweep_icp_result` sets `error_message` on the response; `finalize_inner` returns early.
6. `try_auto_finalize` stores the failed response in `auto_finalize_swap_response` and returns `Ok(failed_response)`.
7. On the next periodic task invocation, `can_auto_finalize()` returns `Err` because `already_tried_to_auto_finalize` is `true`.
8. `requires_periodic_tasks()` returns `false`; the timer is disabled.
9. Buyer ICP remains locked in the swap canister subaccounts indefinitely. The failed buyer's ICP is never refunded or forwarded, mirroring the `settleDuel` permanent lockup described in the external report. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L669-674)
```rust
    pub fn requires_periodic_tasks(&self) -> bool {
        // Practically, already_tried_to_auto_finalize should never be None, unless a Swap has not
        // been updated since this field had been introduced. We default this field to `true` to
        // capture those old Swaps (which were finalized manually).
        !self.lifecycle_is_terminal() || !self.already_tried_to_auto_finalize.unwrap_or(true)
    }
```

**File:** rs/sns/swap/src/swap.rs (L704-736)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1557-1561)
```rust
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
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

**File:** rs/sns/swap/src/types.rs (L895-902)
```rust
    pub fn set_sweep_icp_result(&mut self, sweep_icp_result: SweepResult) {
        if !sweep_icp_result.is_successful_sweep() {
            self.set_error_message(
                "Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization".to_string()
            );
        }
        self.sweep_icp_result = Some(sweep_icp_result);
    }
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L637-654)
```text
message TransferableAmount {
  // The amount in e8s equivalent that the participant committed to the Swap,
  // which is held by the swap canister until the swap is committed or aborted.
  uint64 amount_e8s = 1;

  // When the transfer to refund or commit funds starts.
  uint64 transfer_start_timestamp_seconds = 2;

  // When the transfer to refund or commit succeeds.
  uint64 transfer_success_timestamp_seconds = 3;

  // The amount that was successfully transferred when swap commits or aborts
  // (minus fees).
  optional uint64 amount_transferred_e8s = 4;

  // The fee charged when transferring from the swap canister;
  optional uint64 transfer_fee_paid_e8s = 5;
}
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L17-33)
```markdown
## Motivation

Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
