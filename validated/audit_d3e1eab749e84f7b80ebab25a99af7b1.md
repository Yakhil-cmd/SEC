### Title
Permissionless `execute_scheduled` in XCC Router Enables Temporary Fund Freeze via Front-Running - (File: etc/xcc-router/src/lib.rs)

### Summary
The `execute_scheduled` function in the XCC router contract is explicitly callable by any NEAR account. When called, it atomically removes the scheduled promise from storage and dispatches it — regardless of whether the underlying promise ultimately succeeds or fails. An attacker can front-run a user's `execute_scheduled` call, triggering the promise at an adversarially chosen moment (e.g., after manipulating a DEX the promise targets). If the promise fails, the user's tokens remain locked in the router contract and the promise entry is permanently gone, preventing retry until the user schedules a new recovery promise.

### Finding Description
The `schedule` function in `etc/xcc-router/src/lib.rs` is restricted to the parent (Aurora Engine):

```rust
pub fn schedule(&mut self, #[serializer(borsh)] promise: PromiseArgs) {
    self.assert_preconditions();          // only parent can call
    let nonce = self.nonce.get().unwrap_or_default();
    self.scheduled_promises.insert(nonce, promise);
    self.nonce.set(&(nonce + 1));
    near_sdk::log!("Promise scheduled at nonce {}", nonce);  // nonce is public
}
```

The nonce assigned to each promise is emitted as a public on-chain log. The `execute_scheduled` function, however, has no caller restriction:

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

The comment's security reasoning is incomplete. The removal of the promise from `scheduled_promises` is committed to NEAR storage the moment `execute_scheduled` returns successfully. The promise itself executes asynchronously. If the asynchronous promise fails (e.g., the target contract panics due to slippage, a paused state, or any other condition), the promise actions are reverted but the `scheduled_promises.remove` is **not** rolled back — the entry is permanently gone.

**Attack flow:**

1. User A's EVM contract calls the XCC precompile with `CrossContractCallArgs::Delayed(promise)`, causing the Aurora Engine to call `schedule` on the router sub-account. The router assigns nonce `N` and emits `"Promise scheduled at nonce N"` on-chain.
2. The attacker observes the log and reads the promise contents (target contract, method, args).
3. The attacker manipulates the target contract's state (e.g., front-runs a DEX to move the price outside the promise's slippage tolerance, or drains a storage deposit so the target panics).
4. The attacker calls `execute_scheduled(N)` on the router contract before User A.
5. `scheduled_promises.remove(&N)` is committed. The promise is dispatched.
6. The promise fails at the target contract.
7. User A's tokens (e.g., NEP-141 tokens held by the router) remain in the router contract.
8. User A calls `execute_scheduled(N)` — it panics with `ERR_PROMISE_NOT_FOUND`.
9. User A must schedule a new recovery promise via the XCC precompile to retrieve the tokens, paying additional gas and NEAR.

The attacker can repeat this pattern on every recovery attempt, sustaining the freeze.

### Impact Explanation
**High — Temporary freezing of funds.** User tokens held in the XCC router sub-account are inaccessible until the user successfully schedules and executes a new recovery promise. Each recovery attempt can be griefed by the same front-running pattern. While the freeze is not permanent (the user can eventually succeed if the attacker stops or the target contract state normalises), the attacker can sustain it indefinitely at low cost (only NEAR gas per front-run call).

### Likelihood Explanation
**Medium.** The attacker requires:
1. Knowledge of the scheduled nonce — trivially obtained from the public on-chain log emitted by `schedule`.
2. Ability to put the target contract into a failing state — realistic for any DEX, AMM, or token contract where the attacker can front-run a swap or drain a storage deposit.
3. Ability to call `execute_scheduled` before the user — straightforward since NEAR transaction ordering within a block is observable and the function has no caller restriction.

The XCC feature is designed for EVM contracts interacting with arbitrary NEAR contracts, making DEX/AMM targets common in practice.

### Recommendation
Restrict `execute_scheduled` to the parent account (Aurora Engine) only, mirroring the access control on `execute` and `schedule`. If open execution is desired for liveness reasons, add a caller-controlled lock: only the EVM address whose sub-account this router belongs to (derivable from the account ID prefix) should be permitted to trigger execution. Alternatively, record the intended executor in the scheduled promise entry and enforce it at execution time.

### Proof of Concept

```
1. User A's EVM contract calls XCC precompile:
     CrossContractCallArgs::Delayed(PromiseArgs::Create(PromiseCreateArgs {
         target_account_id: "some-dex.near",
         method: "swap",
         args: b"{\"min_out\": \"1000\"}",
         attached_balance: Yocto::new(0),
         attached_gas: NearGas::new(50_000_000_000_000),
     }))

2. Aurora Engine calls router.schedule(promise).
   Router emits: "Promise scheduled at nonce 0"

3. Attacker reads nonce=0 from chain logs.

4. Attacker front-runs the DEX: moves price so swap output < min_out=1000.

5. Attacker calls:
     router_account.execute_scheduled({"nonce": "0"})
   → scheduled_promises.remove(0) committed.
   → Promise dispatched to DEX.
   → DEX panics: "ERR_MIN_AMOUNT".
   → Promise fails; token balances in router unchanged.

6. User A calls execute_scheduled({"nonce": "0"})
   → panics: "ERR_PROMISE_NOT_FOUND"

7. User A's tokens remain in router sub-account.
   Attacker repeats step 4-6 on every recovery attempt.
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** etc/xcc-router/src/lib.rs (L50-62)
```rust
pub struct Router {
    /// The account id of the Aurora Engine instance that controls this router.
    parent: LazyOption<AccountId>,
    /// The version of the router contract that was last deployed
    version: LazyOption<u32>,
    /// A sequential id to keep track of how many scheduled promises this router has executed.
    /// This allows multiple promises to be scheduled before any of them are executed.
    nonce: LazyOption<u64>,
    /// The storage for the scheduled promises.
    scheduled_promises: LookupMap<u64, PromiseArgs>,
    /// Account ID for the wNEAR contract.
    wnear_account: AccountId,
}
```

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
