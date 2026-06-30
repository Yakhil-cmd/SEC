### Title
Permissionless `execute_scheduled` in XCC Router Allows Third Parties to Execute Scheduled Promises Out of Order, Causing Temporary Fund Freeze — (File: `etc/xcc-router/src/lib.rs`)

---

### Summary

The `execute_scheduled` function on the XCC router contract is intentionally permissionless and accepts a caller-controlled `nonce` parameter. When a user's EVM contract schedules multiple dependent promises, any third party can execute them out of order by choosing which nonce to supply. Because the promise is **removed from storage before the NEAR promise executes**, a failed out-of-order execution permanently destroys the scheduled entry, forcing the user to re-schedule and temporarily freezing their intended operation.

---

### Finding Description

Every EVM address that uses the XCC precompile is assigned a dedicated NEAR sub-account (`{address}.aurora`) running the XCC router contract. The router stores scheduled promises in a `LookupMap<u64, PromiseArgs>` keyed by a sequential nonce. The `schedule` function is gated to the parent (Aurora Engine): [1](#0-0) 

However, `execute_scheduled` is explicitly open to any caller: [2](#0-1) 

The root cause is the ordering of operations inside `execute_scheduled`:

1. `self.scheduled_promises.remove(&nonce.0)` — **commits the removal to NEAR state**
2. `Self::promise_create(promise)` — schedules the NEAR cross-contract call
3. `env::promise_return(promise_id)` — returns

Because the current function succeeds (no panic), the removal is finalized regardless of whether the downstream promise succeeds or fails. NEAR does not revert state changes from a successfully-completed function when a subsequent promise fails. A third party who calls `execute_scheduled(nonce=1)` before `execute_scheduled(nonce=0)` causes promise 1 to fail (if it depends on promise 0), and that entry is **permanently erased** from `scheduled_promises`. [3](#0-2) 

The developer comment acknowledges the permissionless design but incorrectly assumes all scheduled promises are independent: [4](#0-3) 

---

### Impact Explanation

**High — Temporary freezing of funds.**

When an EVM contract schedules a sequence of dependent promises (e.g., `approve` then `swap`, or `unstake` then `withdraw`), a third party can execute the dependent promise first. The dependent promise fails at the NEAR runtime level, its entry is permanently removed from `scheduled_promises`, and the user's intended operation is not performed. The user must re-schedule the promise via a new EVM transaction through the XCC precompile and wait for it to be executed, temporarily freezing their assets or intended cross-chain operation. This is the direct analog to the reported vulnerability: a permissionless finalization function where the caller controls a parameter (nonce) that determines which user action is executed, overriding the user's intended ordering.

---

### Likelihood Explanation

Any unprivileged NEAR account can call `execute_scheduled` on any router sub-account (`{address}.aurora`). Nonces are sequential starting from 0 and are emitted via `near_sdk::log!` on every `schedule` call: [5](#0-4) 

An attacker can monitor NEAR blockchain events, identify router accounts with multiple pending scheduled promises, and deliberately execute a later nonce before an earlier one to disrupt the user's intended sequence. No special privileges, leaked keys, or governance capture are required.

---

### Recommendation

Restrict `execute_scheduled` to be callable only by the parent account (Aurora Engine), consistent with the access control applied to `execute` and `schedule`: [6](#0-5) 

If permissionless execution is a design requirement, enforce sequential ordering — only allow executing nonce `N` if all nonces `< N` have already been executed — or store a "minimum executable nonce" pointer that advances monotonically.

---

### Proof of Concept

1. Alice's EVM contract (address `0xALICE`) calls the XCC precompile twice, causing the Aurora Engine to call `schedule` on `0xALICE.aurora`:
   - **Nonce 0**: `ft_transfer_call` on a token contract to approve 100 tokens for a DEX
   - **Nonce 1**: `swap` on the DEX (requires the approval from nonce 0 to succeed)

2. Bob (any NEAR account) observes the log `"Promise scheduled at nonce 1"` on-chain and immediately calls:
   ```
   0xALICE.aurora::execute_scheduled({"nonce": "1"})
   ```

3. Inside `execute_scheduled`:
   - Nonce 1's `PromiseArgs` is **removed** from `scheduled_promises` (state committed)
   - The swap promise is created and dispatched to the DEX

4. The DEX's `swap` call fails because the token approval (nonce 0) has not been executed yet.

5. Nonce 1 is permanently gone from `scheduled_promises`. Alice cannot re-execute it.

6. Alice's 100 tokens remain locked in the router contract. She must issue a new EVM transaction through the XCC precompile to re-schedule the swap, temporarily freezing her intended cross-chain operation. [7](#0-6)

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
