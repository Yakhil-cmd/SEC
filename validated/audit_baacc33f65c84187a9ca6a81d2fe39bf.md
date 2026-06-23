### Title
`MAX_CANISTER_BALANCE` Limit Not Enforced in `ic0_msg_cycles_accept128` and Bypassable via Cycle Refunds — (`rs/interfaces/src/execution_environment.rs`)

---

### Summary

The IC protocol specification defines `MAX_CANISTER_BALANCE` as the maximum number of cycles a canister on an application subnet may hold. The `ic0_msg_cycles_accept` and `ic0_msg_cycles_accept128` system APIs are documented to enforce this ceiling. However, the production implementation does not check the limit at the accept site, and the cycle-refund path that returns unaccepted cycles to a calling canister also applies no ceiling check. This is directly analogous to the reported ERC-314 `maxWallet` bypass: a per-entity accumulation cap is enforced at one entry point but is silently absent at a second, equally reachable path.

---

### Finding Description

The interface contract for `ic0_msg_cycles_accept128` (and its deprecated 64-bit sibling `ic0_msg_cycles_accept`) states:

> "The canister balance afterwards does not exceed maximum amount of cycles it can hold (public spec refers to this constant as `MAX_CANISTER_BALANCE`) minus any possible outstanding balances. However, canisters on system subnets have no balance limit."
>
> **EXE-117: the last point is not properly handled yet. In particular, a refund can come back to the canister after this call finishes which causes the canister's balance to overflow.** [1](#0-0) 

The actual implementation of `msg_cycles_accept` in the sandbox-safe system state performs no comparison against any balance ceiling — it simply adds the accepted amount to the running balance:

```rust
pub(super) fn msg_cycles_accept(&mut self, amount_to_accept: Cycles) -> Cycles {
    let mut new_balance = self.cycles_balance();
    // ... scales by call-context availability only ...
    new_balance += amount_to_accept;
    self.update_balance_change(new_balance);
    amount_to_accept
}
``` [2](#0-1) 

The second bypass path is the refund applied when a response arrives. `ResponseHelper::apply_initial_refunds` calls `system_state.add_cycles(self.refund_for_sent_cycles)` unconditionally: [3](#0-2) 

`add_cycles` itself is a bare increment with no ceiling guard:

```rust
pub fn add_cycles(&mut self, amount: Cycles) {
    self.cycles_balance += amount;
}
``` [4](#0-3) 

The two missing checks mirror the ERC-314 pattern exactly:

| ERC-314 | IC |
|---|---|
| `buy()` checks `maxWallet` | `ic0_msg_cycles_accept128` should check `MAX_CANISTER_BALANCE` — but does not |
| `transfer()` has no `maxWallet` check | `add_cycles` (refund path) has no `MAX_CANISTER_BALANCE` check |

---

### Impact Explanation

An unprivileged canister on an application subnet can accumulate a cycles balance exceeding `MAX_CANISTER_BALANCE` by:

1. Sending a large inter-canister call with cycles attached to a cooperating (or self-controlled) canister.
2. Having that callee return the cycles as a refund (by not accepting them).
3. Simultaneously calling `ic0_msg_cycles_accept128` to accept cycles from a separate incoming call up to the current balance ceiling.
4. When the refund arrives, `add_cycles` pushes the balance above `MAX_CANISTER_BALANCE` with no rejection.

Alternatively, a canister can simply call `ic0_msg_cycles_accept128` repeatedly across multiple messages — since no check is performed, the balance grows without bound. The intended resource-accounting invariant (application-subnet canisters are bounded in cycles held) is broken, undermining the economic model that makes cycles a scarce resource on application subnets.

---

### Likelihood Explanation

The bypass requires only normal inter-canister messaging, which any deployed canister can perform. No privileged role, governance majority, or threshold key is needed. The EXE-117 ticket is explicitly open in the codebase comments, confirming the gap is unresolved in production code. Likelihood is **High**.

---

### Recommendation

1. In `msg_cycles_accept` (and `msg_cycles_accept128`) in `sandbox_safe_system_state.rs`, cap `amount_to_accept` so that `new_balance` does not exceed `MAX_CANISTER_BALANCE` (for non-system-subnet canisters).
2. In `apply_initial_refunds` in `response.rs`, after adding `refund_for_sent_cycles`, clamp the resulting balance to `MAX_CANISTER_BALANCE` and burn or drop the excess, consistent with the spec.
3. Add a corresponding check in `add_cycles` or introduce a separate `add_cycles_capped` variant used by all refund paths.
4. Close EXE-117 once the fix is in place.

---

### Proof of Concept

```
Canister A (application subnet, balance = MAX_CANISTER_BALANCE - 1):
  1. Sends call to Canister B with X cycles attached (X > 1).
  2. Canister B replies without accepting any cycles → refund of X cycles
     is queued back to A.
  3. Before the refund is delivered, A also receives an incoming call
     carrying 2 cycles and calls ic0_msg_cycles_accept128(2).
     → No ceiling check; A's balance becomes MAX_CANISTER_BALANCE + 1.
  4. The refund from step 2 then arrives via apply_initial_refunds →
     add_cycles(X) → A's balance becomes MAX_CANISTER_BALANCE + 1 + X.

Result: Canister A holds MAX_CANISTER_BALANCE + 1 + X cycles,
        exceeding the protocol-specified ceiling with no error.
``` [5](#0-4) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/interfaces/src/execution_environment.rs (L1205-1213)
```rust
    /// The canister balance afterwards does not exceed
    /// maximum amount of cycles it can hold (public spec refers to this
    /// constant as MAX_CANISTER_BALANCE) minus any possible outstanding
    /// balances. However, canisters on system subnets have no balance
    /// limit.
    ///
    /// EXE-117: the last point is not properly handled yet.  In particular, a
    /// refund can come back to the canister after this call finishes which
    /// causes the canister's balance to overflow.
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L1017-1055)
```rust
    pub(super) fn msg_cycles_accept(&mut self, amount_to_accept: Cycles) -> Cycles {
        let mut new_balance = self.cycles_balance();

        // It is safe to unwrap since msg_cycles_accept and msg_cycles_accept128 are
        // available only forApiType::{Update, ReplicatedQuery, ReplyCallback,
        // RejectCallBack} and all of them have CallContextId, hence
        // SystemStateModifications::call_context_balance_taken will never be `None`.
        debug_assert!(
            self.system_state_modifications
                .call_context_balance_taken
                .is_some()
        );

        let balance_taken = &mut self
            .system_state_modifications
            .call_context_balance_taken
            .as_mut()
            .unwrap()
            .1;

        // Scale amount that can be accepted by what is actually available on
        // the call context.
        let amount_available = Cycles::from(
            self.call_context_balance
                .unwrap()
                .get()
                .checked_sub(balance_taken.get())
                .unwrap(),
        );

        let amount_to_accept = std::cmp::min(amount_available, amount_to_accept);

        // Withdraw and accept the cycles
        *balance_taken += amount_to_accept;

        new_balance += amount_to_accept;

        self.update_balance_change(new_balance);
        amount_to_accept
```

**File:** rs/execution_environment/src/execution/response.rs (L208-212)
```rust
    fn apply_initial_refunds(&mut self) {
        self.canister
            .system_state
            .add_cycles(self.refund_for_sent_cycles);

```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1985-1987)
```rust
    pub fn add_cycles(&mut self, amount: Cycles) {
        self.cycles_balance += amount;
    }
```
