### Title
XCC Precompile Overcounts `callback_count` via `promise_count()` on `NearPromise::And`, Causing Systematic EVM Gas Overcharge — (`engine-precompiles/src/xcc.rs`)

### Summary

The `CrossContractCallArgs::Eager` path in the XCC precompile computes `callback_count` as `call.promise_count() - 1` and uses it to scale the NEAR gas attached to the router's `execute` call. However, `NearPromise::And`'s `promise_count()` sums all parallel sub-promise counts rather than counting only `Then`-style callbacks. This causes `callback_count` — and therefore the EVM gas charged to the caller — to be systematically overstated whenever the `And` combinator is used.

### Finding Description

In `engine-precompiles/src/xcc.rs`, the `Eager` branch computes:

```rust
let callback_count = call
    .promise_count()
    .checked_sub(1)
    .ok_or_else(|| ExitError::Other(Cow::from(consts::ERR_INVALID_INPUT)))?;
let router_exec_cost = costs::ROUTER_EXEC_BASE
    + NearGas::new(callback_count * costs::ROUTER_EXEC_PER_CALLBACK.as_u64());
``` [1](#0-0) 

`call.promise_count()` for `PromiseArgs::Recursive` delegates to `NearPromise::promise_count()`:

```rust
pub fn promise_count(&self) -> u64 {
    match self {
        Self::Simple(_) => 1,
        Self::Then { base, .. } => base.promise_count() + 1,
        Self::And(ps) => ps.iter().map(Self::promise_count).sum(),
    }
}
``` [2](#0-1) 

For `NearPromise::And(ps)`, `promise_count()` returns the **sum of all sub-promise counts** — treating each parallel branch as if it were a sequential callback. But `And` creates zero callbacks; it only fans out parallel promises. The `ROUTER_EXEC_PER_CALLBACK` cost is intended to cover the overhead of each `Then`-style callback the router must process, not each parallel branch.

**Concrete miscalculation:**

| Input | `promise_count()` | `callback_count` | Actual callbacks | Overcount |
|---|---|---|---|---|
| `And([p1, p2])` | 2 | 1 | 0 | +1 |
| `And([p1, p2, p3])` | 3 | 2 | 0 | +2 |
| `Then { base: And([p1, p2]), callback: c }` | 3 | 2 | 1 | +1 |
| `And([p1, ..., pN])` | N | N−1 | 0 | N−1 |

The overcounted `router_exec_cost` is then added to `promise.attached_gas`, and the EVM gas charged to the caller is:

```rust
cost += EthGas::new(promise.attached_gas.as_u64() / costs::CROSS_CONTRACT_CALL_NEAR_GAS);
``` [3](#0-2) 

With `ROUTER_EXEC_PER_CALLBACK = 12_000_000_000_000` NEAR gas and `CROSS_CONTRACT_CALL_NEAR_GAS = 175_000_000`:

- Overcharge per extra parallel promise = `12_000_000_000_000 / 175_000_000 ≈ 68,571` EVM gas
- For `And([p1, ..., p10])`: overcharge ≈ `9 × 68,571 = 617,143` EVM gas [4](#0-3) 

### Impact Explanation

Users who submit `CrossContractCallArgs::Eager(PromiseArgs::Recursive(NearPromise::And([...])))` are charged more EVM gas than the actual computation warrants. Since EVM gas is deducted from the caller's ETH balance at `effective_gas_price`, the excess is a direct, non-refundable loss of ETH. The loss scales linearly with the number of parallel promises in the `And` combinator and is bounded only by the user-supplied `gas_limit`. This is a systematic overcharge of user funds in motion.

**Impact: High — Theft of user funds (excess ETH paid as gas fees).**

### Likelihood Explanation

The `And` combinator is a documented, first-class feature of the XCC system. A workspace integration test (`test_xcc_and_combinator`) exercises it via `Delayed`, but `Eager` accepts the same `PromiseArgs::Recursive` variant and is the primary execution path. Any Aurora EVM user who calls the XCC precompile with an `Eager` `And`-containing promise is affected without any special privileges.

**Likelihood: High** — the affected code path is reachable by any unprivileged EVM caller using the intended XCC interface.

### Recommendation

Replace `promise_count() - 1` with a dedicated function that counts only `Then`-style callbacks (i.e., the number of `Then` nodes in the promise tree), not the total number of leaf promises. For example:

```rust
impl NearPromise {
    pub fn callback_count(&self) -> u64 {
        match self {
            Self::Simple(_) => 0,
            Self::Then { base, .. } => base.callback_count() + 1,
            Self::And(ps) => ps.iter().map(Self::callback_count).sum(),
        }
    }
}
```

Then in the precompile:

```rust
// Before (buggy):
let callback_count = call.promise_count().checked_sub(1)...;

// After (correct):
let callback_count = match &call {
    PromiseArgs::Recursive(p) => p.callback_count(),
    PromiseArgs::Callback(_) => 1,
    PromiseArgs::Create(_) => 0,
};
``` [5](#0-4) 

### Proof of Concept

1. Craft an `Eager` XCC call with `PromiseArgs::Recursive(NearPromise::And([p1, p2, ..., p10]))` where each `pi` is a `Simple(Create(...))`.
2. `promise_count()` returns 10; `callback_count` = 9.
3. `router_exec_cost` = `ROUTER_EXEC_BASE + 9 × ROUTER_EXEC_PER_CALLBACK` = `7 TGas + 9 × 12 TGas` = `115 TGas`.
4. Actual router overhead for `And` with no callbacks = `ROUTER_EXEC_BASE` = `7 TGas`.
5. EVM gas overcharge = `(115 - 7) × 10^12 / 175_000_000` = `108 × 10^12 / 175_000_000 ≈ 617,143` EVM gas.
6. At any non-zero `gas_price`, the caller loses `617,143 × gas_price` Wei of ETH that is not refunded. [6](#0-5) [7](#0-6)

### Citations

**File:** engine-precompiles/src/xcc.rs (L36-48)
```rust
    pub const CROSS_CONTRACT_CALL_BASE: EthGas = EthGas::new(343_650);
    /// Additional EVM gas cost per bytes of input given.
    /// See `CROSS_CONTRACT_CALL_BASE` for estimation methodology.
    pub const CROSS_CONTRACT_CALL_BYTE: EthGas = EthGas::new(4);
    /// EVM gas cost per NEAR gas attached to the created promise.
    /// This value is derived from the gas report `https://hackmd.io/@birchmd/Sy4piXQ29`
    /// The units on this quantity are `NEAR Gas / EVM Gas`.
    /// The report gives a value `0.175 T(NEAR_gas) / k(EVM_gas)`. To convert the units to
    /// `NEAR Gas / EVM Gas`, we simply multiply `0.175 * 10^12 / 10^3 = 175 * 10^6`.
    pub const CROSS_CONTRACT_CALL_NEAR_GAS: u64 = 175_000_000;

    pub const ROUTER_EXEC_BASE: NearGas = NearGas::new(7_000_000_000_000);
    pub const ROUTER_EXEC_PER_CALLBACK: NearGas = NearGas::new(12_000_000_000_000);
```

**File:** engine-precompiles/src/xcc.rs (L139-157)
```rust
        let (promise, attached_near) = match args {
            CrossContractCallArgs::Eager(call) => {
                let call_gas = call.total_gas();
                let attached_near = call.total_near();
                let callback_count = call
                    .promise_count()
                    .checked_sub(1)
                    .ok_or_else(|| ExitError::Other(Cow::from(consts::ERR_INVALID_INPUT)))?;
                let router_exec_cost = costs::ROUTER_EXEC_BASE
                    + NearGas::new(callback_count * costs::ROUTER_EXEC_PER_CALLBACK.as_u64());
                let promise = PromiseCreateArgs {
                    target_account_id,
                    method: consts::ROUTER_EXEC_NAME.into(),
                    args: borsh::to_vec(&call)
                        .map_err(|_| ExitError::Other(Cow::from(consts::ERR_SERIALIZE)))?,
                    attached_balance: ZERO_YOCTO,
                    attached_gas: router_exec_cost.saturating_add(call_gas),
                };
                (promise, attached_near)
```

**File:** engine-precompiles/src/xcc.rs (L174-174)
```rust
        cost += EthGas::new(promise.attached_gas.as_u64() / costs::CROSS_CONTRACT_CALL_NEAR_GAS);
```

**File:** engine-types/src/parameters/promise.rs (L19-25)
```rust
    pub fn promise_count(&self) -> u64 {
        match self {
            Self::Create(_) => 1,
            Self::Callback(_) => 2,
            Self::Recursive(p) => p.promise_count(),
        }
    }
```

**File:** engine-types/src/parameters/promise.rs (L114-122)
```rust
impl NearPromise {
    #[must_use]
    pub fn promise_count(&self) -> u64 {
        match self {
            Self::Simple(_) => 1,
            Self::Then { base, .. } => base.promise_count() + 1,
            Self::And(ps) => ps.iter().map(Self::promise_count).sum(),
        }
    }
```
