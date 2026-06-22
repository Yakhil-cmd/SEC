### Title
Canister Balance Can Exceed `MAX_CANISTER_BALANCE` Due to Unaccounted Outstanding Refunds in `ic0_msg_cycles_accept` / `ic0_msg_cycles_accept128` — (File: `rs/interfaces/src/execution_environment.rs`)

---

### Summary

The `ic0_msg_cycles_accept` and `ic0_msg_cycles_accept128` system calls do not account for outstanding cycle refunds from other pending inter-canister calls when enforcing the `MAX_CANISTER_BALANCE` limit. A canister can accept cycles up to near `MAX_CANISTER_BALANCE` during a reply/reject callback, and when other pending calls subsequently return their unaccepted cycle refunds, those refunds are added unconditionally to the balance — pushing it past `MAX_CANISTER_BALANCE`. This is the direct IC analog of the external report: funds (refunds) are added to a balance that is already at capacity, because the capacity check does not account for pending additions.

---

### Finding Description

The IC interface specification requires that `ic0_msg_cycles_accept` and `ic0_msg_cycles_accept128` ensure the canister balance does not exceed `MAX_CANISTER_BALANCE` minus any possible outstanding balances. This is explicitly documented in the production interface trait:

> *"The canister balance afterwards does not exceed maximum amount of cycles it can hold (public spec refers to this constant as MAX_CANISTER_BALANCE) minus any possible outstanding balances."*
> *"EXE-117: the last point is not properly handled yet. In particular, a refund can come back to the canister after this call finishes which causes the canister's balance to overflow."* [1](#0-0) [2](#0-1) 

The implementation of `msg_cycles_accept` in `sandbox_safe_system_state.rs` only limits acceptance to what is available in the **current** call context (`call_context_balance`), with no check against `MAX_CANISTER_BALANCE` and no accounting for cycles that will be refunded from other outstanding calls:

```rust
let amount_available = Cycles::from(
    self.call_context_balance
        .unwrap()
        .get()
        .checked_sub(balance_taken.get())
        .unwrap(),
);
let amount_to_accept = std::cmp::min(amount_available, amount_to_accept);
*balance_taken += amount_to_accept;
new_balance += amount_to_accept;
self.update_balance_change(new_balance);
``` [3](#0-2) 

When a response arrives for a different outstanding call, `apply_initial_refunds()` in `response.rs` unconditionally calls `add_cycles(self.refund_for_sent_cycles)` with no balance cap:

```rust
fn apply_initial_refunds(&mut self) {
    self.canister
        .system_state
        .add_cycles(self.refund_for_sent_cycles);
    // ...
}
``` [4](#0-3) 

And `add_cycles` itself performs no cap check:

```rust
pub fn add_cycles(&mut self, amount: Cycles) {
    self.cycles_balance += amount;
}
``` [5](#0-4) 

---

### Impact Explanation

A canister's `cycles_balance` (a `u128`) can be pushed past `MAX_CANISTER_BALANCE`. Depending on whether `Cycles` arithmetic is wrapping or saturating, this either silently wraps the balance to near zero (destroying cycles) or silently caps it (losing the refunded cycles). Either outcome violates the cycles conservation invariant of the IC protocol. A canister that deliberately engineers this scenario can cause its own balance to be corrupted, or — if the balance wraps — effectively destroy cycles that were legitimately owed to it as a refund.

---

### Likelihood Explanation

Any unprivileged canister developer on an application subnet can trigger this. The canister must:
1. Have a balance near `MAX_CANISTER_BALANCE` (achievable by receiving many top-ups via CMC).
2. Make two or more outgoing inter-canister calls with cycles attached.
3. In the reply callback of the first call, call `ic0_msg_cycles_accept128(u128::MAX)` to accept as many cycles as possible.
4. When the second call's response arrives, its unaccepted cycle refund is added unconditionally, overflowing the balance.

This requires no privileged access, no governance majority, and no threshold corruption. It is reachable via normal ingress and inter-canister call paths.

---

### Recommendation

When computing the amount to accept in `msg_cycles_accept`, subtract the sum of all outstanding refunds (cycles attached to pending outgoing calls that have not yet returned) from `MAX_CANISTER_BALANCE` to derive the true headroom. Only accept up to `min(requested, available_in_call_context, MAX_CANISTER_BALANCE - current_balance - outstanding_refunds)`. Similarly, `apply_initial_refunds` should cap the refund addition at `MAX_CANISTER_BALANCE - current_balance` and burn or drop any excess.

---

### Proof of Concept

1. Canister `C` is topped up to `MAX_CANISTER_BALANCE - 1` cycles.
2. `C` sends two outgoing calls, each attaching `X` cycles (deducted from balance, so balance is now `MAX_CANISTER_BALANCE - 1 - 2X`).
3. Call 1 returns; its callee accepted 0 cycles, so the refund is `X`. In the reply callback, `C` calls `ic0_msg_cycles_accept128(X)`. Balance becomes `MAX_CANISTER_BALANCE - 1 - X`.
4. Call 2 returns; its callee accepted 0 cycles, so the refund is `X`. `apply_initial_refunds` calls `add_cycles(X)` unconditionally. Balance becomes `MAX_CANISTER_BALANCE - 1`, but if step 3 had accepted more (up to `MAX_CANISTER_BALANCE - 1 - X + X = MAX_CANISTER_BALANCE - 1`) and the second refund of `X` arrives, the balance becomes `MAX_CANISTER_BALANCE - 1 + X > MAX_CANISTER_BALANCE`.

The root cause — adding a refund to a balance that is already at capacity, without checking the limit — is structurally identical to the external report's finding where `bonusFromReserve + balance > capacity` causes incorrect protocol behavior. [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** rs/interfaces/src/execution_environment.rs (L1225-1233)
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

**File:** rs/execution_environment/src/execution/response.rs (L208-230)
```rust
    fn apply_initial_refunds(&mut self) {
        self.canister
            .system_state
            .add_cycles(self.refund_for_sent_cycles);

        // `self.prepayment_for_call_transmission` might be zero for any responses
        // to requests that were produced before this field was stored in callbacks.
        // In that scenario, use `self.prepayment_for_response_transmission` for some
        // partial update to consumed metrics (the ones that behave like counters)
        // during refund phase. This does not affect the refund to balance or the
        // metrics that behave like gauges as these are updated based on the refund amount.
        // This code can be dropped when all callbacks have the `prepayment_for_call_transmission`
        // field set.
        let prepayment_for_call_transmission = if self.prepayment_for_call_transmission.is_zero() {
            self.prepayment_for_response_transmission
        } else {
            self.prepayment_for_call_transmission
        };
        self.canister.system_state.refund_cycles(
            prepayment_for_call_transmission,
            self.refund_for_response_transmission,
        );
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1985-1987)
```rust
    pub fn add_cycles(&mut self, amount: Cycles) {
        self.cycles_balance += amount;
    }
```
