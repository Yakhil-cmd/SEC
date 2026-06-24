### Title
Unchecked `.unwrap()` on `bounded_wait` Self-Call in `get_node_providers_rewards` Can Block Monthly Node Provider Reward Distribution - (File: rs/node_rewards/canister/src/canister/mod.rs)

---

### Summary

Inside `NodeRewardsCanister::get_node_providers_rewards`, a `bounded_wait` self-call to the no-op `reset_instructions` method is made once per day in the reward period, and its result is unconditionally `.unwrap()`-ed. If this self-call fails for any reason — cycles exhaustion, canister stopped/frozen, or a bounded-wait timeout — the entire function panics/traps. Because NNS Governance calls this function as part of `mint_monthly_node_provider_rewards`, a panic here permanently blocks the monthly node provider reward distribution for that cycle, directly analogous to the ComplexRewarder child-rewarder failure freezing user withdrawals.

---

### Finding Description

In `get_node_providers_rewards`, the canister iterates over every day in the requested reward period. After computing each day's rewards it makes a self-call to reset the instruction counter:

```rust
// rs/node_rewards/canister/src/canister/mod.rs  lines 341-347
#[cfg(target_arch = "wasm32")]
let _ = ic_cdk::call::Call::bounded_wait(
    ic_cdk::api::canister_self(),
    "reset_instructions",
)
.await
.unwrap();          // ← panics on any call failure
```

`reset_instructions` is a hidden no-op query:

```rust
// rs/node_rewards/canister/src/main.rs  line 110-111
#[query(hidden = true)]
fn reset_instructions() {}
```

The `bounded_wait` variant carries an implicit deadline. If the Node Rewards Canister is low on cycles, is temporarily stopped, or the subnet is congested enough to exceed the deadline, the call returns `Err(...)`. The `.unwrap()` then causes an unconditional `panic!`, trapping the entire `get_node_providers_rewards` update call.

The call chain from NNS Governance is:

```
mint_monthly_node_provider_rewards()          [governance.rs:4067-4071]
  └─ get_node_providers_rewards()             [governance.rs:7650]
       └─ get_node_providers_xdr_permyriad_rewards()  [governance.rs:7538]
            └─ inter-canister call → Node Rewards Canister
                 └─ NodeRewardsCanister::get_node_providers_rewards()
                      └─ for each day: bounded_wait("reset_instructions").unwrap()  ← TRAP
```

Because `mint_monthly_node_provider_rewards` propagates the error via `?` after the inter-canister call, a trap in the Node Rewards Canister causes the entire monthly reward minting to fail for that invocation.

---

### Impact Explanation

Monthly node provider reward distribution is permanently stalled for the affected period. Node providers do not receive their ICP compensation. Because `mint_monthly_node_provider_rewards` is driven by a timer and the lock is released on return, the next timer tick will retry — but if the underlying condition (e.g., cycles exhaustion) persists, every retry will also trap, indefinitely blocking distribution. This is a direct governance/economic impact on the NNS: ICP that should be minted and transferred to node providers is never minted.

---

### Likelihood Explanation

The Node Rewards Canister makes one self-call per day in the reward period. For a 30-day period this is 30 sequential inter-canister calls, each consuming cycles. If the canister's cycle balance is insufficient to sustain all 30 calls, the `bounded_wait` will fail mid-loop. Additionally, `bounded_wait` carries a finite timeout; under subnet congestion the self-call may not be scheduled within the deadline. Both conditions are realistic in production, especially as the reward period grows or if the canister's top-up cadence lags.

---

### Recommendation

Replace the unconditional `.unwrap()` with graceful error handling. If the instruction-reset self-call fails, the function should either log and continue (since the call is purely a performance optimization to avoid hitting the instruction limit) or propagate a recoverable `Err`:

```rust
#[cfg(target_arch = "wasm32")]
if let Err(e) = ic_cdk::call::Call::bounded_wait(
    ic_cdk::api::canister_self(),
    "reset_instructions",
)
.await
{
    ic_cdk::println!("reset_instructions self-call failed: {:?}; continuing", e);
    // do not panic — reward calculation must not be blocked by a non-critical helper call
}
```

Because `reset_instructions` is a no-op whose sole purpose is to yield execution and reset the instruction counter, a failure should never abort the reward calculation.

---

### Proof of Concept

1. NNS Governance timer fires and calls `mint_monthly_node_provider_rewards` with `are_performance_based_rewards_enabled() == true`. [1](#0-0) 

2. Governance makes an inter-canister call to the Node Rewards Canister's `get_node_providers_rewards` update method (caller-gated to Governance only). [2](#0-1) 

3. Inside `NodeRewardsCanister::get_node_providers_rewards`, the loop iterates over each day in the reward period and, on every iteration, calls `bounded_wait("reset_instructions").unwrap()`. [3](#0-2) 

4. If the Node Rewards Canister's cycle balance is exhausted (or the bounded-wait deadline is exceeded), the self-call returns `Err(...)`. The `.unwrap()` panics, trapping the update call.

5. The trap propagates as a rejection back to NNS Governance. `mint_monthly_node_provider_rewards` returns an error; `update_most_recent_monthly_node_provider_rewards` is never called; no ICP is minted for node providers this month. [4](#0-3) 

6. The timer retries on the next tick. If the cycle condition persists, every retry traps identically, permanently blocking distribution.

### Citations

**File:** rs/nns/governance/src/governance.rs (L4067-4071)
```rust
        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };
```

**File:** rs/nns/governance/src/governance.rs (L4073-4076)
```rust
        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
```

**File:** rs/nns/governance/src/governance.rs (L7549-7569)
```rust
        let response: Vec<u8> = self
            .env
            .call_canister_method(
                NODE_REWARDS_CANISTER_ID,
                "get_node_providers_rewards",
                Encode!(&GetNodeProvidersRewardsRequest {
                    from_day: start_date,
                    to_day: end_date,
                    algorithm_version: None
                })
                .unwrap(),
            )
            .await
            .map_err(|(code, msg)| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error calling 'get_node_providers_rewards': code: {code:?}, message: {msg}"
                    ),
                )
            })?;
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L341-347)
```rust
            #[cfg(target_arch = "wasm32")]
            let _ = ic_cdk::call::Call::bounded_wait(
                ic_cdk::api::canister_self(),
                "reset_instructions",
            )
            .await
            .unwrap();
```
