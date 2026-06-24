### Title
`can_auto_finalize` Permanently Blocks SNS Swap Auto-Finalization When `already_tried_to_auto_finalize` Is `None` - (File: rs/sns/swap/src/swap.rs)

### Summary
In `rs/sns/swap/src/swap.rs`, the `can_auto_finalize` function uses `self.already_tried_to_auto_finalize.unwrap_or(true)` to guard against repeated auto-finalization attempts. When the optional field is `None` — which occurs for any swap canister whose state predates the introduction of this field — the guard evaluates to `true` and permanently blocks auto-finalization, even though no attempt has ever been made. This is the direct IC analog of the `fromStage(Stages.Completed)` bug: the guard that is supposed to allow the function in the correct state actually prevents it.

### Finding Description
`can_auto_finalize` is called by `run_periodic_tasks` on every heartbeat to decide whether to automatically finalize a committed or aborted swap:

```rust
// rs/sns/swap/src/swap.rs
pub fn can_auto_finalize(&self) -> Result<(), String> {
    self.can_finalize()?;
    ...
    // Fail early if we've already tried to auto-finalize the swap.
    if self.already_tried_to_auto_finalize.unwrap_or(true) {   // ← bug
        return Err(format!(
            "self.already_tried_to_auto_finalize is {:?}, indicating that an attempt \
             has already been made to auto-finalize. ...",
            self.already_tried_to_auto_finalize
        ));
    }
    Ok(())
}
``` [1](#0-0) 

`already_tried_to_auto_finalize` is declared as an `optional bool` in protobuf, so it is `None` for any swap canister whose persisted state was written before this field was added. When `None`, `unwrap_or(true)` returns `true`, the `if` branch fires, and the function returns an error claiming auto-finalization was already attempted — even though it never was.

The caller in `run_periodic_tasks` silently discards this error:

```rust
else if self.can_auto_finalize().is_ok() {
    // ... attempt auto-finalization
}
``` [2](#0-1) 

So for every heartbeat, the swap silently skips auto-finalization. The swap remains stuck in `COMMITTED` or `ABORTED` indefinitely until someone manually calls `finalize`.

The field is defined as optional in the generated Rust struct:

```rust
#[prost(bool, optional, tag = "17")]
pub already_tried_to_auto_finalize: ::core::option::Option<bool>,
``` [3](#0-2) 

The correct default for a field meaning "has this been attempted?" is `false` (not yet attempted), i.e., `unwrap_or(false)`. Using `unwrap_or(true)` inverts the semantics for the `None` case.

### Impact Explanation
Any SNS swap canister whose stable state has `already_tried_to_auto_finalize = None` will never auto-finalize after reaching `COMMITTED` or `ABORTED`. SNS token buyers cannot receive their SNS neurons automatically, and ICP refunds are not issued automatically. The swap is permanently stuck until an external caller manually invokes `finalize`. This is a governance/lifecycle denial-of-service for the auto-finalization path.

### Likelihood Explanation
The `already_tried_to_auto_finalize` field is an optional protobuf field added after the initial swap canister deployment. Any swap canister upgraded from a version that did not include this field will have `None` in its persisted state. Because IC canister upgrades preserve existing state and protobuf optional fields default to absent, this condition is reachable for any swap that was live before the field was introduced. The entry path requires no special privilege: the periodic task fires automatically on every heartbeat.

### Recommendation
Change the default from `true` to `false`:

```diff
- if self.already_tried_to_auto_finalize.unwrap_or(true) {
+ if self.already_tried_to_auto_finalize.unwrap_or(false) {
```

`None` means the field was never written, which is semantically equivalent to "not yet attempted." The conservative default should be `false` so that swaps with missing state are allowed to auto-finalize rather than being permanently blocked.

### Proof of Concept

1. Deploy an SNS swap canister from a version that does not include the `already_tried_to_auto_finalize` field (or manually set the field to `None` in state).
2. Upgrade the canister to the current version. The field remains `None` in persisted state.
3. Allow the swap to reach `COMMITTED` or `ABORTED`.
4. Observe that `run_periodic_tasks` fires on every heartbeat but `can_auto_finalize()` always returns `Err("self.already_tried_to_auto_finalize is None, indicating that an attempt has already been made...")`.
5. The swap never auto-finalizes. Participants must wait for a manual `finalize` call.

The test at `rs/sns/swap/tests/swap.rs` confirms that `already_tried_to_auto_finalize` is expected to be `Some(false)` at swap open time, but makes no assertion about the `None` case, leaving the `unwrap_or(true)` path untested. [4](#0-3)

### Citations

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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L226-228)
```rust
    /// from being attempted more than once.
    #[prost(bool, optional, tag = "17")]
    pub already_tried_to_auto_finalize: ::core::option::Option<bool>,
```

**File:** rs/sns/swap/tests/swap.rs (L1131-1139)
```rust
    assert_eq!(swap.lifecycle(), Open);
    assert_eq!(swap.already_tried_to_auto_finalize, Some(false));
    let auto_finalization_error = swap
        .try_auto_finalize(now_fn, &mut spy_clients_exploding_root())
        .await
        .unwrap_err();
    let allowed_to_finalize_error = swap.can_finalize().unwrap_err();
    assert_eq!(auto_finalization_error, allowed_to_finalize_error);
    assert_eq!(swap.already_tried_to_auto_finalize, Some(false));
```
