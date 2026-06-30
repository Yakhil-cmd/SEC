### Title
Unprivileged Premature Execution of Scheduled XCC Promises Enables Theft of Unclaimed Yield — (`etc/xcc-router/src/lib.rs`)

---

### Summary

The `execute_scheduled` function in the XCC router contract is intentionally callable by any NEAR account with no access control. An attacker can force premature execution of a victim's scheduled cross-contract call at a time chosen by the attacker rather than the victim, causing the victim to lose yield they would have earned by waiting for optimal conditions (e.g., a reward multiplier reaching its maximum value in an external DeFi protocol).

---

### Finding Description

When a user invokes the XCC precompile with `CrossContractCallArgs::Delayed`, the promise is stored in their personal XCC router sub-account via `Router::schedule`, which is gated to the parent (Aurora Engine): [1](#0-0) 

The stored promise is then executable by **anyone** via `execute_scheduled`: [2](#0-1) 

The developer comment at line 146–148 asserts "there is no security risk" because the promise content is fixed. This reasoning is correct for *what* the promise does, but it ignores *when* it executes. The victim may have deliberately chosen `Delayed` execution to wait for an optimal moment — for example, until a reward multiplier in an external DeFi protocol reaches its maximum before claiming. An attacker can call `execute_scheduled(nonce)` at any block, forcing execution at a suboptimal time.

The promise is removed from storage atomically on execution: [3](#0-2) 

Once removed and executed, the victim cannot re-schedule or undo the premature claim. The attacker pays only NEAR gas (no stake required).

The `CrossContractCallArgs::Delayed` variant is explicitly designed for cases where the user needs a fresh 300 Tgas budget in a future transaction, making it a primary path for expensive DeFi interactions such as reward claims: [4](#0-3) 

---

### Impact Explanation

**High — Theft of unclaimed yield.**

If the scheduled promise is a reward-claim call to an external DeFi protocol that applies a time-based multiplier (the exact pattern described in H-02), premature execution locks in a lower multiplier. The victim permanently loses the incremental yield they would have received by waiting. The loss is proportional to the gap between the current multiplier and the maximum multiplier, multiplied by the accumulated unaccrued rewards. The attacker can repeat this for every nonce the victim schedules.

---

### Likelihood Explanation

**High.** The function is unconditionally public on every deployed XCC router sub-account. Any NEAR account can enumerate router sub-accounts (they follow the deterministic pattern `{evm_address}.{aurora_engine_account}`), discover pending nonces from on-chain logs (`"Promise scheduled at nonce {}"`) emitted by `schedule`, and call `execute_scheduled` with zero permission requirements. The only cost to the attacker is NEAR gas.

---

### Recommendation

1. **Restrict `execute_scheduled` to the owner or parent.** Add a caller check analogous to `assert_preconditions` so only the Aurora Engine (parent) or the EVM address that owns the router can trigger execution. If open execution is desired for liveness, add a user-configurable earliest-execution timestamp per nonce.
2. **Alternatively, add a per-nonce time-lock.** Store a `not_before` block height alongside each scheduled promise and enforce it inside `execute_scheduled`.

---

### Proof of Concept

1. Alice uses the XCC precompile with `CrossContractCallArgs::Delayed` to schedule a promise that calls `claim_rewards` on an external NEAR DeFi contract. She intends to wait 30 days until the reward multiplier reaches 2×.
2. On day 15, Bob observes the log `"Promise scheduled at nonce 0"` emitted from Alice's router (`{alice_evm_address}.aurora`).
3. Bob calls `execute_scheduled({"nonce": "0"})` on Alice's router. The function has no caller check: [5](#0-4) 
4. The `claim_rewards` promise executes at day 15 with a 1.5× multiplier instead of 2×.
5. Alice's nonce-0 promise is deleted from `scheduled_promises`. She cannot re-schedule the same claim.
6. Alice permanently loses the 0.5× incremental yield on her accumulated rewards. Bob can repeat this for every future nonce Alice schedules.

### Citations

**File:** etc/xcc-router/src/lib.rs (L135-144)
```rust
    /// Similar security considerations here as for `execute`.
    pub fn schedule(&mut self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

        let nonce = self.nonce.get().unwrap_or_default();
        self.scheduled_promises.insert(nonce, promise);
        self.nonce.set(&(nonce + 1));

        near_sdk::log!("Promise scheduled at nonce {}", nonce);
    }
```

**File:** etc/xcc-router/src/lib.rs (L146-156)
```rust
    /// It is intentional that this function can be called by anyone (not just the parent).
    /// There is no security risk to allowing this function to be open because it can only
    /// act on promises that were created via `schedule`.
    #[payable]
    pub fn execute_scheduled(&mut self, nonce: U64) {
        let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
            env::panic_str("ERR_PROMISE_NOT_FOUND")
        };
        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

**File:** engine-types/src/parameters/promise.rs (L279-285)
```rust
    /// The promise is to be stored in the router contract, and can be executed in a future transaction.
    /// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
    /// For example, if an expensive EVM call ends with a NEAR cross contract call, then there may not be
    /// much gas left to perform it. In this case, the promise could be `Delayed` (stored in the router)
    /// and executed in a separate transaction with a fresh 300 Tgas available for it.
    Delayed(PromiseArgs),
}
```
