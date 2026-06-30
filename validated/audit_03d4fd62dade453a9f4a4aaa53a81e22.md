### Title
Permissionless `execute_scheduled` Enables Sandwich Attack on User-Scheduled NEAR Swaps - (File: etc/xcc-router/src/lib.rs)

### Summary
The XCC router's `execute_scheduled` function has no access control and can be called by any NEAR account. When a user schedules a price-sensitive cross-contract call (e.g., a token swap on a NEAR DEX such as Ref Finance) via the `CrossContractCallArgs::Delayed` path, an attacker can observe the pending promise on-chain, manipulate the market price, and then trigger `execute_scheduled` at the worst possible moment for the victim — executing a classic sandwich attack that drains the user's swap proceeds.

### Finding Description

The XCC router (`etc/xcc-router/src/lib.rs`) exposes two paths for executing NEAR cross-contract calls from EVM contracts:

- **Eager** (`CrossContractCallArgs::Eager`): calls `execute` on the router, which is gated by `assert_preconditions()` (only the parent Aurora engine may call it).
- **Delayed** (`CrossContractCallArgs::Delayed`): calls `schedule` on the router (also gated), which stores the `PromiseArgs` in `scheduled_promises: LookupMap<u64, PromiseArgs>` under an auto-incremented nonce. Execution is deferred to a later call to `execute_scheduled`.

`execute_scheduled` is explicitly permissionless:

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

The inline comment's security reasoning is incomplete. It is correct that the attacker cannot *create* or *modify* promises — but the attacker can choose *when* to execute them. For any promise that is price-sensitive (a DEX swap, a liquidity operation, etc.), the ability to choose the execution moment is equivalent to the ability to sandwich the transaction.

The `scheduled_promises` map is stored in NEAR contract state, which is publicly readable by any observer. The nonce counter is also readable via `self.nonce`. An attacker can enumerate all pending scheduled promises for any router sub-account.

The XCC precompile in `engine-precompiles/src/xcc.rs` confirms that `Delayed` calls route to `schedule`:

```rust
CrossContractCallArgs::Delayed(call) => {
    let promise = PromiseCreateArgs {
        target_account_id,
        method: consts::ROUTER_SCHEDULE_NAME.into(),
        ...
    };
```

### Impact Explanation

**Critical — Direct theft of user funds.**

A user who schedules a token swap (e.g., "swap 10,000 wNEAR for USDC on Ref Finance") via `CrossContractCallArgs::Delayed` has their swap parameters locked in storage. An attacker who observes this pending promise can:

1. Buy a large amount of USDC on Ref Finance, driving the price up.
2. Call `execute_scheduled(nonce)` — the user's swap now executes at the inflated price, receiving far fewer USDC than expected.
3. Sell USDC at the inflated price, pocketing the difference.

If the user did not encode a tight `min_amount_out` in the swap arguments, the full slippage is extractable. Even with a loose slippage bound, the attacker can extract up to the full tolerance. The router sub-account holds the user's wNEAR (unwrapped from the EVM side before the call executes), so the stolen value comes directly from the user's bridged assets.

### Likelihood Explanation

**High.** The XCC `Delayed` path is a documented, production-facing feature. NEAR contract state is publicly readable, so any attacker can enumerate pending scheduled promises across all router sub-accounts. NEAR DEXes (Ref Finance, etc.) have finite liquidity and are susceptible to price manipulation. No special privilege is required — any NEAR account can call `execute_scheduled`. The attack requires only capital for the price manipulation step, which can itself be funded via a flash loan on the NEAR side.

### Recommendation

Restrict `execute_scheduled` so that only the owner of the router sub-account (i.e., the `parent` Aurora engine, acting on behalf of the EVM address that owns this sub-account) can trigger execution. Alternatively, require that the caller be either the parent or the EVM address whose sub-account this router belongs to. The simplest fix mirrors the pattern already used by `execute` and `schedule`:

```rust
pub fn execute_scheduled(&mut self, nonce: U64) {
    self.assert_preconditions(); // add this line
    let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
        env::panic_str("ERR_PROMISE_NOT_FOUND")
    };
    let promise_id = Self::promise_create(promise);
    env::promise_return(promise_id);
}
```

If the intent is to allow the user themselves (not just the engine) to trigger execution, a separate allowlist of permitted callers (parent engine + the EVM address owner) should be maintained.

### Proof of Concept

1. Alice uses the XCC precompile with `CrossContractCallArgs::Delayed` to schedule a swap: "swap 10,000 wNEAR for USDC on Ref Finance, min_out = 0" (or any loose bound). The promise is stored at nonce `0` in Alice's router sub-account `<alice_addr>.aurora`.

2. Attacker Bob reads Alice's router state on-chain and sees the pending swap parameters.

3. Bob buys a large amount of USDC on Ref Finance, driving the wNEAR/USDC price against Alice.

4. Bob calls:
   ```
   near call <alice_addr>.aurora execute_scheduled '{"nonce": "0"}' --accountId bob.near
   ```
   This is accepted because `execute_scheduled` has no access control. [1](#0-0) 

5. Alice's swap executes at the manipulated price. Alice receives far fewer USDC than the fair-market amount.

6. Bob sells his USDC position at the inflated price, profiting from the spread.

The `schedule` function (which stores the promise) is correctly gated: [2](#0-1) 

The `execute` function (eager path) is also correctly gated: [3](#0-2) 

Only `execute_scheduled` lacks the guard, creating the asymmetry: [4](#0-3) 

The `assert_preconditions` helper that enforces `predecessor == parent` is defined and available: [5](#0-4) 

The XCC precompile confirms the `Delayed` path routes to `schedule`, making this a reachable production code path from any EVM user: [6](#0-5)

### Citations

**File:** etc/xcc-router/src/lib.rs (L123-133)
```rust
    /// This function can only be called by the parent account (i.e. Aurora engine) to ensure that
    /// no one can create calls on behalf of the user this router contract is deployed for.
    /// The engine only calls this function when the special precompile in the EVM for NEAR cross
    /// contract calls is used by the address associated with the sub-account this router contract
    /// is deployed at.
    pub fn execute(&self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

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

**File:** etc/xcc-router/src/lib.rs (L205-208)
```rust
    /// Panics if any of the preconditions checked in `require_preconditions` are not met.
    fn assert_preconditions(&self) {
        self.require_preconditions().unwrap_or_else(env_panic);
    }
```

**File:** engine-precompiles/src/xcc.rs (L159-172)
```rust
            CrossContractCallArgs::Delayed(call) => {
                let attached_near = call.total_near();
                let promise = PromiseCreateArgs {
                    target_account_id,
                    method: consts::ROUTER_SCHEDULE_NAME.into(),
                    args: borsh::to_vec(&call)
                        .map_err(|_| ExitError::Other(Cow::from(consts::ERR_SERIALIZE)))?,
                    attached_balance: ZERO_YOCTO,
                    // We don't need to add any gas to the amount need for the schedule call
                    // since the promise is not executed right away.
                    attached_gas: costs::ROUTER_SCHEDULE,
                };
                (promise, attached_near)
            }
```
