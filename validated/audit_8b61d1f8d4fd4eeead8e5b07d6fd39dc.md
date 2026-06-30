### Title
Permissionless `execute_scheduled` Allows Any Caller to Front-Run and Disrupt a User's Scheduled Cross-Contract Calls - (File: `etc/xcc-router/src/lib.rs`)

---

### Summary

The `execute_scheduled` function in the XCC router contract is explicitly permissionless — callable by any NEAR account. An attacker can monitor the router's on-chain state for pending scheduled promises and call `execute_scheduled` before the legitimate user does, forcing premature execution. If the promise fails at that moment (e.g., because the target contract is in an incompatible state, a time-lock has not elapsed, or a required precondition is unmet), the promise is **permanently removed from storage** while the underlying funds remain inaccessible in the target contract. The attacker can repeat this indefinitely every time the user reschedules, creating a sustained DoS against the user's cross-contract call flow.

---

### Finding Description

In `etc/xcc-router/src/lib.rs`, the `execute_scheduled` function has no caller restriction:

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
``` [1](#0-0) 

The developer comment asserts "there is no security risk" because the promise content is fixed by the user. This reasoning is incomplete: while the attacker cannot alter the promise *content*, they fully control the *timing* of execution.

The critical flaw is the ordering of operations:

1. `self.scheduled_promises.remove(&nonce.0)` — the promise is **permanently deleted from storage** in the current receipt.
2. `Self::promise_create(promise)` — the promise is dispatched as an async NEAR receipt.

Because NEAR's async execution model processes these in separate receipts, if the dispatched promise receipt fails (e.g., the target contract panics, a balance check fails, or a time-lock is active), the storage deletion from step 1 is **not rolled back**. The promise is gone. The user must re-enter the entire XCC flow: submit a new EVM transaction, invoke the XCC precompile, have the engine call `schedule` on the router, and then call `execute_scheduled` again — at which point the attacker can repeat the attack. [2](#0-1) 

The nonce is publicly observable: it is emitted as an on-chain log by `schedule`:

```rust
near_sdk::log!("Promise scheduled at nonce {}", nonce);
``` [3](#0-2) 

The `schedule` function itself is correctly restricted to the parent (Aurora Engine):

```rust
pub fn schedule(&mut self, #[serializer(borsh)] promise: PromiseArgs) {
    self.assert_preconditions();
    ...
}
``` [4](#0-3) 

But `execute_scheduled` has no such guard, creating an asymmetry: only the engine can *create* a scheduled promise, but *anyone* can trigger its execution.

The `Delayed` XCC variant — which is the only path that uses `execute_scheduled` — is specifically designed for cases where the user needs a fresh 300 Tgas budget in a separate transaction:

```rust
/// The promise is to be stored in the router contract, and can be executed in a future transaction.
/// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
Delayed(PromiseArgs),
``` [5](#0-4) 

This design means the user *intentionally* defers execution. An attacker exploiting the window between scheduling and intended execution can disrupt this flow.

---

### Impact Explanation

**High. Temporary freezing of funds.**

When the attacker front-runs `execute_scheduled` and the promise fails:

- The user's scheduled cross-contract call (e.g., an `ft_transfer` or a DEX interaction) is consumed from storage and cannot be retried without rescheduling.
- Funds that were to be moved by the promise remain locked in the target contract, inaccessible to the user until they successfully complete the full XCC flow again.
- Because the attacker can repeat this every time the user reschedules, the disruption can be sustained indefinitely, effectively making the funds permanently inaccessible for as long as the attacker is willing to pay gas.

The router sub-account is derived deterministically from the user's EVM address and the engine account:

```rust
let target_account_id = AccountId::try_from(format!(
    "{}.{}",
    args.target.encode(),
    current_account_id.as_ref()
))?;
``` [6](#0-5) 

This means the attacker can compute the router account ID for any victim and monitor it for scheduled promises.

---

### Likelihood Explanation

**Medium.**

- The nonce is publicly logged on-chain, so the attacker always knows which nonce to target.
- The attacker only needs to submit a NEAR transaction to the router sub-account before the user does — a standard front-running race condition on NEAR.
- The attack is most effective against time-sensitive promises (e.g., those targeting contracts with time-locks, price-window checks, or state preconditions). Such promises are a natural use case for the `Delayed` XCC variant, since they require careful timing.
- The cost to the attacker is only NEAR gas per `execute_scheduled` call, which is low.

---

### Recommendation

Restrict `execute_scheduled` so that only the parent Aurora Engine account (or optionally the user's derived EVM address) can call it. This preserves the intended use case (the engine or user triggers execution in a separate transaction for gas reasons) while eliminating the front-running vector:

```rust
pub fn execute_scheduled(&mut self, nonce: U64) {
    // Only the parent (Aurora Engine) may trigger execution.
    self.assert_preconditions(); // reuse existing parent check
    let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
        env::panic_str("ERR_PROMISE_NOT_FOUND")
    };
    let promise_id = Self::promise_create(promise);
    env::promise_return(promise_id);
}
```

Alternatively, introduce a minimum delay between scheduling and execution (analogous to a cooling period), so that even if anyone can call `execute_scheduled`, they cannot do so immediately after scheduling.

---

### Proof of Concept

1. **Victim** (EVM user with address `0xABCD...`) submits an EVM transaction using the `Delayed` XCC variant targeting a time-locked NEAR contract. The engine calls `schedule` on the router `abcd....aurora`, storing the promise at nonce `0` and emitting `"Promise scheduled at nonce 0"`.

2. **Attacker** observes the log on-chain and immediately calls:
   ```
   router_account.execute_scheduled({"nonce": "0"})
   ```
   before the time-lock on the target contract has elapsed.

3. The router removes the promise from `scheduled_promises` (state committed) and dispatches it. The target contract rejects the call because the time-lock has not elapsed. The async receipt fails.

4. The promise is permanently gone from the router's storage. The victim's funds remain in the target contract.

5. The victim reschedules (new EVM transaction + XCC precompile call). The attacker repeats step 2. This loop continues indefinitely. [1](#0-0) [7](#0-6)

### Citations

**File:** etc/xcc-router/src/lib.rs (L136-144)
```rust
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

**File:** engine-types/src/parameters/promise.rs (L279-284)
```rust
    /// The promise is to be stored in the router contract, and can be executed in a future transaction.
    /// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
    /// For example, if an expensive EVM call ends with a NEAR cross contract call, then there may not be
    /// much gas left to perform it. In this case, the promise could be `Delayed` (stored in the router)
    /// and executed in a separate transaction with a fresh 300 Tgas available for it.
    Delayed(PromiseArgs),
```

**File:** engine/src/xcc.rs (L80-84)
```rust
    let target_account_id = AccountId::try_from(format!(
        "{}.{}",
        args.target.encode(),
        current_account_id.as_ref()
    ))?;
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
