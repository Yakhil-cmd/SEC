### Title
Unnecessary `#[payable]` on `execute_scheduled` Permanently Freezes Attached NEAR — (`File: etc/xcc-router/src/lib.rs`)

---

### Summary

The `execute_scheduled` function in the XCC router contract is marked `#[payable]` but never reads or forwards `env::attached_deposit()`. Because the function is intentionally open to any caller (not just the parent), any NEAR tokens attached to a call are permanently locked in the router contract with no recovery path.

---

### Finding Description

In `etc/xcc-router/src/lib.rs`, `execute_scheduled` carries the `#[payable]` attribute: [1](#0-0) 

The `#[payable]` attribute in NEAR SDK means the runtime will accept an attached deposit without reverting. However, the function body never calls `env::attached_deposit()`, never forwards the deposit to the outgoing promise, and never refunds it to the caller. Any NEAR attached to the call is silently absorbed into the router contract's balance.

The only NEAR-recovery mechanism in the contract is `send_refund`, which unconditionally transfers a hard-coded constant (`REFUND_AMOUNT = 2 NEAR`) to the parent account: [2](#0-1) [3](#0-2) 

`send_refund` is a storage-refund mechanism, not a general withdrawal. It cannot recover arbitrary stuck NEAR. No other function in the contract transfers an arbitrary balance out.

The contract's own comment confirms the function is open to any caller: [4](#0-3) 

The test suite confirms this open-access design by calling `execute_scheduled` from `bob()` (a non-parent account): [5](#0-4) 

---

### Impact Explanation

Any NEAR attached to a call to `execute_scheduled` is permanently frozen in the router contract. There is no admin withdrawal, no sweep function, and `send_refund` only moves a fixed 2 NEAR. The funds cannot be recovered by the sender, the parent engine, or any other party. This constitutes **Critical — Permanent Freezing of Funds**.

---

### Likelihood Explanation

`execute_scheduled` is a public, permissionless entry point on a production NEAR contract. A user or integrating contract that attaches NEAR (e.g., believing the deposit is needed to fund the outgoing cross-contract call) will permanently lose those tokens. The `#[payable]` attribute provides no warning; it actively signals to callers that a deposit is acceptable. Likelihood is **Medium**: the function is reachable by any unprivileged account, and the `#[payable]` marker creates a false expectation that attached value is handled.

---

### Recommendation

Remove the `#[payable]` attribute from `execute_scheduled`. The function does not need to accept deposits; the outgoing promise's attached balance is encoded inside the stored `PromiseArgs` and is drawn from the router's own balance, not from the caller's deposit. Removing `#[payable]` causes the NEAR runtime to automatically reject any call that attaches a non-zero deposit, preventing the freeze entirely.

If there is a legitimate reason to accept a deposit (e.g., to fund the outgoing promise), the function must explicitly read `env::attached_deposit()` and forward it to the promise, or refund it to the caller on completion.

---

### Proof of Concept

1. The parent (Aurora Engine) calls `schedule` to store a `PromiseArgs` at nonce `N` in the router.
2. An unprivileged attacker calls `execute_scheduled(N)` with an attached deposit of, say, 10 NEAR.
3. The NEAR runtime accepts the deposit because `#[payable]` is present.
4. The function removes the stored promise and dispatches it — but never touches `env::attached_deposit()`.
5. The 10 NEAR is now part of the router contract's balance.
6. No function in the router can withdraw an arbitrary amount: `send_refund` only sends the fixed `REFUND_AMOUNT` (2 NEAR) to the parent, and no other transfer path exists.
7. The 10 NEAR is permanently frozen. [6](#0-5)

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

**File:** etc/xcc-router/src/tests.rs (L169-173)
```rust
    // anyone can call this function
    testing_env!(VMContextBuilder::new()
        .predecessor_account_id(bob())
        .build());
    contract.execute_scheduled(0.into());
```
