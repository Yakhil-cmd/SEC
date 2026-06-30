### Title
Unnecessary `#[payable]` on `execute_scheduled` Permanently Traps Attached NEAR in XCC Router — (File: `etc/xcc-router/src/lib.rs`)

---

### Summary

The XCC Router contract's `execute_scheduled` function is marked `#[payable]` but never reads or forwards `env::attached_deposit()`. Any NEAR tokens attached to a call are silently absorbed into the router's account balance. No general recovery function exists in the contract; the only transfer-out method (`send_refund`) sends a fixed 2 NEAR to the parent, not the caller's arbitrary deposit. This is a direct analog of the reported "unnecessary ETH acceptance" class.

---

### Finding Description

In `etc/xcc-router/src/lib.rs`, two functions carry the `#[payable]` attribute:

```rust
#[payable]
pub fn execute_scheduled(&mut self, nonce: U64) {          // line 150
    let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
        env::panic_str("ERR_PROMISE_NOT_FOUND")
    };
    let promise_id = Self::promise_create(promise);
    env::promise_return(promise_id);
}

#[payable]
pub fn deploy_upgrade(&mut self, #[serializer(borsh)] args: DeployUpgradeParams) { // line 161
    self.assert_preconditions();
    ...
}
```

Neither function calls `env::attached_deposit()`, uses the deposit in any promise, or refunds it. In NEAR, without `#[payable]` the runtime panics if NEAR is attached; with it, the NEAR is silently kept by the contract. The only outbound transfer in the contract is `send_refund`, which unconditionally sends the hardcoded constant `REFUND_AMOUNT` (2 NEAR) to the parent — it is not a general recovery path for arbitrary stuck deposits.

`execute_scheduled` is explicitly open to any caller:

> "It is intentional that this function can be called by anyone (not just the parent)." [1](#0-0) 

`deploy_upgrade` is restricted to the parent but is equally unnecessary as `#[payable]` since it attaches `NearToken::default()` (zero) to its inner promise call. [2](#0-1) 

The only outbound transfer, `send_refund`, transfers exactly `REFUND_AMOUNT = 2 NEAR` — not the caller's deposit. [3](#0-2) 

---

### Impact Explanation

Any NEAR attached to `execute_scheduled` by an unprivileged caller is trapped in the router sub-account. The caller has no recourse. Recovery requires the Aurora Engine parent to deploy a contract upgrade (`deploy_upgrade`) that adds a withdrawal path — a governance-level action. Until that upgrade is deployed, the funds are frozen. This satisfies **High: Temporary freezing of funds**.

---

### Likelihood Explanation

`execute_scheduled` is callable by any account. A user interacting with the XCC subsystem (e.g., a dApp or script that attaches NEAR to cover potential storage costs, or a user who misreads the interface) can trigger this. The function is part of the production XCC router deployed as a sub-account for every Aurora user who uses cross-contract calls. Likelihood is **Medium** — accidental attachment is plausible given the function is open and `#[payable]`.

---

### Recommendation

Remove `#[payable]` from both `execute_scheduled` and `deploy_upgrade` in `etc/xcc-router/src/lib.rs`. Neither function requires attached NEAR: `execute_scheduled` only dispatches a pre-scheduled promise, and `deploy_upgrade` attaches zero NEAR to its inner `initialize` call. Removing `#[payable]` causes the NEAR runtime to automatically reject any call that attaches NEAR, preventing accidental fund loss. [4](#0-3) [2](#0-1) 

---

### Proof of Concept

1. Alice deploys an XCC router sub-account by using the Aurora Engine's cross-contract call precompile.
2. Alice (or any third party) calls `execute_scheduled(nonce)` on the router, attaching 1 NEAR.
3. The function removes the scheduled promise and dispatches it — `env::attached_deposit()` is never read.
4. The 1 NEAR is now in the router's account balance.
5. Alice calls `send_refund` — it panics because only the parent (Aurora Engine) can call it.
6. The parent's only recovery path is to call `deploy_upgrade` with a new binary that adds a withdrawal function — a privileged, multi-step governance action.
7. Until that upgrade, Alice's 1 NEAR is frozen. [4](#0-3) [3](#0-2) [5](#0-4)

### Citations

**File:** etc/xcc-router/src/lib.rs (L38-40)
```rust
const WNEAR_REGISTER_AMOUNT: NearToken = NearToken::from_yoctonear(1_250_000_000_000_000_000_000);
/// Must match aurora_engine_precompiles::xcc::state::STORAGE_AMOUNT
const REFUND_AMOUNT: NearToken = NearToken::from_near(2);
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

**File:** etc/xcc-router/src/lib.rs (L160-174)
```rust
    #[payable]
    pub fn deploy_upgrade(&mut self, #[serializer(borsh)] args: DeployUpgradeParams) {
        self.assert_preconditions();

        let promise_id = env::promise_batch_create(&env::current_account_id());
        env::promise_batch_action_deploy_contract(promise_id, &args.code);
        env::promise_batch_action_function_call(
            promise_id,
            INITIALIZE,
            &args.initialize_args,
            NearToken::default(),
            INITIALIZE_GAS,
        );
        env::promise_return(promise_id);
    }
```

**File:** etc/xcc-router/src/lib.rs (L176-184)
```rust
    pub fn send_refund(&self) -> Promise {
        let parent = self.get_parent().unwrap_or_else(env_panic);

        require_caller(&parent)
            .and_then(|_| require_no_failed_promises())
            .unwrap_or_else(env_panic);

        Promise::new(parent).transfer(REFUND_AMOUNT)
    }
```
