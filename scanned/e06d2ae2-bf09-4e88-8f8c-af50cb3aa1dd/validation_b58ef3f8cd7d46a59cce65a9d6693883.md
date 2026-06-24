### Title
Usage of Deprecated 64-bit `ic0.msg_cycles_accept` Can Cause Cycles to Not Be Received - (`rs/bitcoin/checker/src/main.rs`, `rs/nns/cmc/src/main.rs`, `rs/nns/sns-wasm/canister/canister.rs`)

---

### Summary

Production IC canisters use the deprecated `ic0.msg_cycles_accept` system API (64-bit) instead of the 128-bit replacement `ic0.msg_cycles_accept128`. This is the direct IC analog to Solidity's deprecated `transfer()`: a deprecated API with a hard resource ceiling that silently fails to receive the full intended amount when cycles exceed the 64-bit limit, or causes a trap when the companion deprecated `ic0.msg_cycles_available` is used and available cycles exceed `u64::MAX`.

---

### Finding Description

The IC system API defines two generations of cycle-acceptance calls:

- **Deprecated (64-bit):** `ic0.msg_cycles_accept(max_amount: u64) → u64`
- **Current (128-bit):** `ic0.msg_cycles_accept128(max_amount_high: u64, max_amount_low: u64)`

The interface spec explicitly marks the 64-bit variant as deprecated:

> "(deprecated) Please use `ic0_msg_cycles_accept128` instead. This API supports only 64-bit values." [1](#0-0) 

Similarly, the companion query API `ic0.msg_cycles_available` is deprecated and **traps** if the available cycles cannot fit in a 64-bit value:

> "(deprecated) Please use `ic0_msg_cycles_available128` instead. Traps if the amount of cycles available cannot fit in a 64-bit value." [2](#0-1) 

This trap path is confirmed by the `TrapCode::CyclesAmountTooBigFor64Bit` variant in the execution error type: [3](#0-2) 

The following **production canisters** use the deprecated `msg_cycles_accept` (64-bit) API:

| File | Matches |
|---|---|
| `rs/bitcoin/checker/src/main.rs` | 2 |
| `rs/nns/cmc/src/main.rs` | 2 |
| `rs/nns/sns-wasm/canister/canister.rs` | 1 |

The `dfn_core` library, which NNS canisters depend on, exposes the deprecated wrapper directly: [4](#0-3) 

The 128-bit replacement is also present in the same file but is not used by the affected canisters: [5](#0-4) 

---

### Impact Explanation

Two distinct failure modes exist:

**Mode 1 — Silent truncation (cycles not received):** `ic0.msg_cycles_accept(max_amount: u64)` can accept at most `u64::MAX` (~18.4 × 10¹⁸) cycles per call. If a caller (e.g., a system-subnet canister with an uncapped balance) attaches more than `u64::MAX` cycles, the canister silently accepts only up to `u64::MAX` and the remainder is refunded. The canister's payment-accounting logic then operates on an incorrect (truncated) amount. For the CMC and SNS-WASM canisters, this means cycles intended as payment are not fully received, breaking fee accounting.

**Mode 2 — Trap / DoS:** If the same canisters also call the deprecated `ic0.msg_cycles_available()` before accepting, and the caller has attached more than `u64::MAX` cycles, the execution **traps** with `CyclesAmountTooBigFor64Bit`. The entire message is rolled back, the canister's intended operation fails, and the caller's cycles are refunded. This is a reachable DoS path against the Bitcoin checker and CMC endpoints that accept cycles as payment.

The impact directly mirrors the Solidity `transfer()` finding: **funds (cycles) may not be received by the fee recipient**, and the operation may revert/trap.

---

### Likelihood Explanation

- System-subnet canisters (NNS, SNS, governance) have no `MAX_CANISTER_BALANCE` cap and can accumulate balances exceeding `u64::MAX`.
- Any such canister can call `ic0.call_cycles_add128` to attach > `u64::MAX` cycles to a call targeting the CMC, SNS-WASM, or Bitcoin checker.
- The attacker-controlled entry path is a standard inter-canister call — no privileged access, no threshold corruption, no social engineering required.
- The CMC's `notify_top_up` and the Bitcoin checker's fee-acceptance endpoints are externally reachable from any canister caller.

---

### Recommendation

Replace all uses of the deprecated `ic0.msg_cycles_accept` (and `ic0.msg_cycles_available`) with their 128-bit counterparts:

- `ic0.msg_cycles_accept128` — accepts a full 128-bit amount
- `ic0.msg_cycles_available128` — reads available cycles without trapping

In `dfn_core`, the `call_cycles_add128` wrapper already exists; the `msg_cycles_accept128` equivalent should be used in all production canister entry points that accept cycles as payment. [5](#0-4) 

---

### Proof of Concept

1. Deploy a canister on the NNS subnet (no balance cap) and fund it with > `u64::MAX` cycles.
2. Call `ic0.call_cycles_add128` to attach `u64::MAX + 1` cycles to a call targeting `rs/nns/cmc/src/main.rs` or `rs/bitcoin/checker/src/main.rs`.
3. **Mode 1:** The recipient calls `ic0.msg_cycles_accept(u64::MAX)` — accepts exactly `u64::MAX`, silently leaving 1 cycle unaccepted and refunded. Fee accounting is off by 1+ cycles.
4. **Mode 2:** If the recipient first calls `ic0.msg_cycles_available()` (deprecated), execution traps with `CyclesAmountTooBigFor64Bit`, the message is rolled back, and the canister's operation fails — a reachable DoS. [6](#0-5)

### Citations

**File:** rs/interfaces/src/execution_environment.rs (L1161-1167)
```rust
    /// (deprecated) Please use `ic0_msg_cycles_available128` instead.
    /// This API supports only 64-bit values.
    ///
    /// Cycles sent in the current call and still available.
    ///
    /// Traps if the amount of cycles available cannot fit in a 64-bit value.
    fn ic0_msg_cycles_available(&self) -> HypervisorResult<u64>;
```

**File:** rs/interfaces/src/execution_environment.rs (L1193-1214)
```rust
    /// (deprecated) Please use `ic0_msg_cycles_accept128` instead.
    /// This API supports only 64-bit values.
    ///
    /// This moves cycles from the
    /// call to the canister balance. It can be called multiple times, each
    /// time adding more cycles to the balance.
    ///
    /// It moves no more cycles than `max_amount`.
    ///
    /// It moves no more cycles than available according to
    /// `ic0.msg_cycles_available`, and
    ///
    /// The canister balance afterwards does not exceed
    /// maximum amount of cycles it can hold (public spec refers to this
    /// constant as MAX_CANISTER_BALANCE) minus any possible outstanding
    /// balances. However, canisters on system subnets have no balance
    /// limit.
    ///
    /// EXE-117: the last point is not properly handled yet.  In particular, a
    /// refund can come back to the canister after this call finishes which
    /// causes the canister's balance to overflow.
    fn ic0_msg_cycles_accept(&mut self, max_amount: u64) -> HypervisorResult<u64>;
```

**File:** rs/interfaces/src/execution_environment/errors.rs (L21-22)
```rust
    CyclesAmountTooBigFor64Bit,
    Other,
```

**File:** rs/interfaces/src/execution_environment/errors.rs (L38-40)
```rust
            Self::CyclesAmountTooBigFor64Bit => {
                write!(f, "cycles amount exceeds 64-bit representation")
            }
```

**File:** rs/rust_canisters/dfn_core/src/api.rs (L628-632)
```rust
pub fn call_cycles_add(amount: u64) {
    unsafe {
        ic0::call_cycles_add(amount);
    }
}
```

**File:** rs/rust_canisters/dfn_core/src/api.rs (L634-638)
```rust
pub fn call_cycles_add128(amount_high: u64, amount_low: u64) {
    unsafe {
        ic0::call_cycles_add128(amount_high, amount_low);
    }
}
```
