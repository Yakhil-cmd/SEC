### Title
Unchecked Empty `NearPromise::And` Vector Causes Panic in XCC Router, Freezing User Funds - (File: `etc/xcc-router/src/lib.rs`)

### Summary
The `NearPromise::And(Vec<Self>)` variant is deserialized and processed in the XCC router without any minimum-length check. An attacker can submit a `CrossContractCallArgs` containing `PromiseArgs::Recursive(NearPromise::And([]))` via the XCC precompile. When the router executes this, it calls `env::promise_and(&[])` with an empty slice, which panics in the NEAR runtime. Because the EVM-side balance debit and NEAR withdrawal from wNEAR occur in a prior committed NEAR transaction, the NEAR transferred to the router becomes permanently unrecoverable.

### Finding Description

`NearPromise::And` is defined as: [1](#0-0) 

Its `BorshDeserialize` implementation reads the inner `Vec<Self>` with no minimum-length enforcement: [2](#0-1) 

In the XCC router, `recursive_promise_create` handles the `And` variant by collecting promise indices and passing them directly to `env::promise_and`: [3](#0-2) 

No guard exists to reject an empty `promises` vector before calling `env::promise_and(&indices)`. The NEAR runtime requires at least one promise index; passing zero causes a host-level panic, reverting the NEAR transaction.

### Impact Explanation

The XCC flow for a `Delayed` call is split across two NEAR transactions:

1. **EVM transaction (`submit`)**: The XCC precompile debits the user's EVM balance, withdraws the required NEAR from wNEAR, and stores the `PromiseArgs` in the router's state. This NEAR transaction commits permanently.
2. **Router execution (`execute_scheduled`)**: The stored `PromiseArgs` are retrieved and `recursive_promise_create` is called. If the args contain `NearPromise::And([])`, `env::promise_and(&[])` panics, reverting only this second NEAR transaction.

Because the NEAR was already transferred to the router in step 1 (committed state), and the stored promise args will always cause a panic on any future `execute_scheduled` call, the NEAR is permanently frozen in the router with no recovery path. This constitutes **permanent freezing of funds** (Critical).

### Likelihood Explanation

Any unprivileged EVM user can call the XCC precompile with a crafted `CrossContractCallArgs` Borsh payload encoding `PromiseArgs::Recursive(NearPromise::And([]))`. The `BorshDeserialize` implementation for `NearPromise` accepts this without error: [4](#0-3) 

No upstream validation in the XCC precompile or engine rejects an empty `And` list before it reaches the router. The attack requires only the ability to submit an EVM transaction, which is available to any user.

### Recommendation

Add a minimum-length check when deserializing or processing `NearPromise::And`. Specifically:

1. In `BorshDeserialize for NearPromise`, after reading the `Vec<Self>` for the `And` variant, return an `io::Error` if the vector is empty.
2. In `recursive_promise_create`, add an explicit guard before calling `env::promise_and`:

```rust
NearPromise::And(promises) => {
    if promises.is_empty() {
        env::panic_str("ERR_EMPTY_AND_PROMISE");
    }
    let indices: Vec<PromiseIndex> = promises
        .iter()
        .map(Self::recursive_promise_create)
        .collect();
    env::promise_and(&indices)
}
```

Similarly, validate the length in the XCC precompile before accepting the `CrossContractCallArgs`.

### Proof of Concept

1. Construct a Borsh-encoded `CrossContractCallArgs::Delayed(PromiseArgs::Recursive(NearPromise::And([])))`.
2. Submit an EVM transaction calling the XCC precompile with this payload, attaching sufficient NEAR (e.g., 1 yoctoNEAR for the call).
3. The EVM transaction commits: the user's EVM balance is debited, NEAR is withdrawn from wNEAR and transferred to the router, and the empty `And` promise args are stored in the router's scheduled storage.
4. Call `execute_scheduled` on the router. The call reaches `recursive_promise_create` → `env::promise_and(&[])` → NEAR runtime panic → transaction reverts.
5. The NEAR transferred in step 3 remains in the router with no mechanism to recover it, as every future `execute_scheduled` call for this nonce will produce the same panic. [5](#0-4) [1](#0-0)

### Citations

**File:** engine-types/src/parameters/promise.rs (L102-112)
```rust
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NearPromise {
    Simple(SimpleNearPromise),
    Then {
        base: Box<Self>,
        // Near doesn't allow arbitrary promises in the callback,
        // only simple calls to contracts or batches of actions.
        callback: SimpleNearPromise,
    },
    And(Vec<Self>),
}
```

**File:** engine-types/src/parameters/promise.rs (L178-207)
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
```

**File:** etc/xcc-router/src/lib.rs (L242-281)
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
```
