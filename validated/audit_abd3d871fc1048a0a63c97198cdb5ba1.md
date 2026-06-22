### Title
SNS Swap Start Timestamp Bypass via `None` Fallback in `can_open` - (`File: rs/sns/swap/src/swap.rs`)

### Summary

The `can_open` function in the SNS Swap canister uses `.unwrap_or(now_seconds)` when reading `decentralization_sale_open_timestamp_seconds`. If that field is `None` (unset), the guard trivially evaluates to `now_seconds >= now_seconds`, which is always `true`. This mirrors the original report's root cause exactly: a timestamp check that passes both when the epoch has ended *and* when the start timestamp has never been set.

### Finding Description

In `rs/sns/swap/src/swap.rs`, the `can_open` function is:

```rust
pub fn can_open(&self, now_seconds: u64) -> bool {
    if self.lifecycle() != Lifecycle::Adopted {
        return false;
    }

    let swap_open_timestamp_seconds = self
        .decentralization_sale_open_timestamp_seconds
        .unwrap_or(now_seconds);   // <-- BUG: None collapses to "open immediately"
    now_seconds >= swap_open_timestamp_seconds
}
``` [1](#0-0) 

In the one-proposal SNS initialization flow, `Swap::new()` sets `decentralization_sale_open_timestamp_seconds` directly from `init.swap_start_timestamp_seconds`:

```rust
res.decentralization_sale_open_timestamp_seconds = init.swap_start_timestamp_seconds;
res.lifecycle = Lifecycle::Adopted as i32;
``` [2](#0-1) 

`swap_start_timestamp_seconds` is declared `optional` in the protobuf schema: [3](#0-2) 

If `swap_start_timestamp_seconds` is absent from the `Init` message (i.e., `None`), the swap enters `Lifecycle::Adopted` with `decentralization_sale_open_timestamp_seconds = None`. On the very next heartbeat, `run_periodic_tasks` calls `try_open(now_seconds)`, which calls `can_open(now_seconds)`. Because `None.unwrap_or(now_seconds)` yields `now_seconds`, the condition `now_seconds >= now_seconds` is always `true`, and the swap transitions to `Lifecycle::Open` immediately — bypassing the intended minimum 24-hour delay enforced at the NNS governance layer. [4](#0-3) 

The integration tests confirm the design intent: the open timestamp must be at least 24 hours in the future after proposal adoption. [5](#0-4) 

### Impact Explanation

An SNS swap that opens before the mandatory delay allows participants to commit ICP before the community has had the required review window. The Neurons' Fund maturity is already deducted at proposal adoption time, so an early open directly affects real ICP-equivalent value. The SNS governance canister remains in `PreInitializationSwap` mode and cannot process normal proposals until the swap concludes, so an early or manipulated swap timeline can lock SNS governance for the full swap duration.

### Likelihood Explanation

The NNS governance `CreateServiceNervousSystem` proposal validation is the primary gate that is expected to enforce a future `swap_start_timestamp_seconds`. However, the swap canister itself performs no such enforcement — it silently accepts `None` and opens immediately. Any path that installs a swap canister with `swap_start_timestamp_seconds = None` (e.g., a future code path, a direct canister install by a developer during testing on mainnet, or a validation gap in NNS governance) triggers the bypass automatically via the heartbeat, with no further attacker action required.

### Recommendation

Change `can_open` to treat a missing `decentralization_sale_open_timestamp_seconds` as "not yet openable" rather than "open immediately":

```rust
pub fn can_open(&self, now_seconds: u64) -> bool {
    if self.lifecycle() != Lifecycle::Adopted {
        return false;
    }

    // If the open timestamp has not been set, the swap cannot be opened yet.
    let Some(swap_open_timestamp_seconds) =
        self.decentralization_sale_open_timestamp_seconds
    else {
        return false;
    };

    now_seconds >= swap_open_timestamp_seconds
}
```

This matches the recommendation in the original report: check the end/open timestamp only, and fail closed when it is absent.

### Proof of Concept

1. Deploy a swap canister via the one-proposal flow with `swap_start_timestamp_seconds` omitted from `Init`.
2. `Swap::new()` sets `lifecycle = Adopted` and `decentralization_sale_open_timestamp_seconds = None`.
3. On the next heartbeat, `run_periodic_tasks` calls `try_open(now)`.
4. `can_open(now)` evaluates `None.unwrap_or(now) = now`; `now >= now` is `true`.
5. The swap transitions to `Lifecycle::Open` immediately, before any community review window has elapsed.
6. Participants can now commit ICP to the swap, and the SNS governance is locked in `PreInitializationSwap` mode for the full swap duration. [6](#0-5)

### Citations

**File:** rs/sns/swap/src/swap.rs (L446-448)
```rust
            res.decentralization_sale_open_timestamp_seconds = init.swap_start_timestamp_seconds;
            // Transit to the next SNS lifecycle state.
            res.lifecycle = Lifecycle::Adopted as i32;
```

**File:** rs/sns/swap/src/swap.rs (L1029-1038)
```rust
        // Automatically transition the state. Only one state transition per periodic task.

        // Auto-open the swap
        if self.try_open(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap opened at timestamp {}",
                periodic_task_start_seconds
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L2866-2875)
```rust
    pub fn can_open(&self, now_seconds: u64) -> bool {
        if self.lifecycle() != Lifecycle::Adopted {
            return false;
        }

        let swap_open_timestamp_seconds = self
            .decentralization_sale_open_timestamp_seconds
            .unwrap_or(now_seconds);
        now_seconds >= swap_open_timestamp_seconds
    }
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L382-384)
```text
  // The date/time when the swap should start.
  optional uint64 swap_start_timestamp_seconds = 22;

```

**File:** rs/sns/integration_tests/src/initialization_flow.rs (L413-420)
```rust
    // Assert that the timestamp of the Swap is at least 24 hours in the future
    let now = sns_initialization_flow_test.now_seconds();
    assert!(
        get_lifecycle_response
            .decentralization_sale_open_timestamp_seconds
            .unwrap()
            >= now + ONE_DAY_SECONDS
    );
```
