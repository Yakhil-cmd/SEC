### Title
Missing Refund Callback on Failed `withdraw` Promise in `ExitToEthereum` ETH Path — (`engine-precompiles/src/native.rs`)

### Summary

`ExitToEthereum::run` with `flag=0x0` deducts the caller's EVM ETH balance via `context.apparent_value` and schedules a bare `PromiseArgs::Create` to call `withdraw` on the eth connector. There is no error-handling callback. If the NEAR-level `withdraw` promise fails, the ETH is permanently lost with no refund path.

### Finding Description

**Entrypoint:** Any EVM transaction that calls the `ExitToEthereum` precompile at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` with `flag=0x0` and a non-zero ETH value attached.

**Execution trace:**

1. The EVM call transfers `context.apparent_value` from the caller's balance to the precompile address. This state change is committed as part of the EVM execution, before any NEAR promise runs.

2. Inside `ExitToEthereum::run` (flag=0x0 branch), the code builds a `PromiseCreateArgs` targeting the eth connector's `withdraw` method: [1](#0-0) 

3. The promise is wrapped as a bare `PromiseArgs::Create` — no callback, no error handler: [2](#0-1) 

4. `filter_promises_from_logs` in the engine schedules this promise directly via `schedule_promise`, again with no error handler attached: [3](#0-2) 

5. The `error_refund` feature — which adds a `PromiseArgs::Callback` with `exit_to_near_precompile_callback` for `ExitToNear` — is **entirely absent** from `ExitToEthereum`. The feature is defined but never wired into the Ethereum exit path: [4](#0-3) [5](#0-4) 

**Conditions under which `withdraw` can fail (without requiring admin compromise):**

- **Serialization mismatch**: `get_withdraw_serialize_type` reads `EthConnectorStorageId::WithdrawSerializationType` from storage and defaults to `Borsh` if absent. If the eth connector contract is upgraded to expect JSON while the storage key still encodes Borsh (or vice versa), the `withdraw` call panics on the connector side and the promise fails. [6](#0-5) [7](#0-6) 

- **Connector-level pause**: The eth connector contract itself may have a pause mechanism independent of Aurora's precompile-level pause (`PrecompileFlags::EXIT_TO_ETHEREUM`). Aurora's precompile pause only prevents the EVM call from reaching the precompile; it does not protect against the connector being paused at the NEAR level after the EVM call has already committed the balance deduction. [8](#0-7) 

### Impact Explanation

The ETH deducted from the caller's EVM balance is committed to state before the NEAR promise executes. If the promise fails, the ETH is permanently stuck at the precompile address with no recovery path. This constitutes **permanent freezing of funds** (Critical), which is within the allowed impact scope. The question labels it "theft of unclaimed yield" (High), but the actual severity is higher — the principal itself is frozen, not merely future yield.

### Likelihood Explanation

The serialization mismatch path is the most realistic trigger: it requires no admin action, only a connector upgrade that changes the expected argument format while the Aurora-side storage key lags behind. The connector-level pause path requires an operator action on the connector contract, but does not require compromise of Aurora's own admin keys. Both paths are reachable through normal operational events.

### Recommendation

Mirror the `error_refund` pattern already present in `ExitToNear`: replace `PromiseArgs::Create(withdraw_promise)` with `PromiseArgs::Callback(PromiseWithCallbackArgs { base: withdraw_promise, callback: refund_callback })`, where the callback calls a new engine method (e.g., `exit_to_ethereum_precompile_callback`) that checks the promise result and, on failure, credits the original `context.apparent_value` back to `context.caller` in the EVM state.

### Proof of Concept

```
1. Deploy Aurora locally.
2. Mock the eth connector so its `withdraw` method always returns a failure
   (e.g., by deploying a stub contract at the connector account ID that panics).
3. Fund an EVM address A with 1 ETH on Aurora.
4. From address A, call ExitToEthereum precompile with:
     input = [0x00] ++ <20-byte Ethereum recipient>
     value = 1 ETH
5. Observe: A's EVM balance is now 0.
6. The NEAR promise to `withdraw` executes and fails (stub panics).
7. Assert: A's EVM balance remains 0 — no refund was issued.
8. Assert: no `exit_to_ethereum_precompile_callback` was ever scheduled
   (because PromiseArgs::Create carries no callback).
```

The invariant "a failed connector withdrawal must refund the EVM ETH balance" is broken. The ETH is permanently lost.

### Citations

**File:** engine-precompiles/src/native.rs (L322-331)
```rust
fn get_withdraw_serialize_type<I: IO>(io: &I) -> Result<WithdrawSerializeType, ExitError> {
    io.read_storage(&construct_contract_key(
        EthConnectorStorageId::WithdrawSerializationType,
    ))
    .map_or(Ok(WithdrawSerializeType::Borsh), |value| {
        value
            .to_value()
            .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_DESERIALIZE")))
    })
}
```

**File:** engine-precompiles/src/native.rs (L449-483)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
        let attached_gas = if method == "ft_transfer_call" {
            costs::FT_TRANSFER_CALL_GAS
        } else {
            costs::FT_TRANSFER_GAS
        };

        let transfer_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method,
            args: args.into_bytes(),
            attached_balance: Yocto::new(1),
            attached_gas,
        };

        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
```

**File:** engine-precompiles/src/native.rs (L897-900)
```rust
                let serialize_fn = match get_withdraw_serialize_type(&self.io)? {
                    WithdrawSerializeType::Json => json_args,
                    WithdrawSerializeType::Borsh => borsh_args,
                };
```

**File:** engine-precompiles/src/native.rs (L977-985)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
```

**File:** engine/src/engine.rs (L1654-1664)
```rust
                            PromiseArgs::Create(promise) => {
                                // Safety: this promise creation is safe because it does not come from
                                // users directly. The exit precompile only create promises which we
                                // are able to execute without violating any security invariants.
                                let id = match previous_promise {
                                    Some(base_id) => {
                                        schedule_promise_callback(handler, base_id, &promise)
                                    }
                                    None => schedule_promise(handler, &promise),
                                };
                                previous_promise = Some(id);
```

**File:** engine/Cargo.toml (L48-48)
```text
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** engine/src/pausables.rs (L9-35)
```rust
bitflags! {
    /// Wraps unsigned integer where each bit identifies a different precompile.
    #[derive(BorshSerialize, BorshDeserialize, Default)]
    #[borsh(crate = "aurora_engine_types::borsh")]
    pub struct PrecompileFlags: u32 {
        const EXIT_TO_NEAR        = 0b01;
        const EXIT_TO_ETHEREUM    = 0b10;
    }
}

impl PrecompileFlags {
    #[must_use]
    pub fn from_address(address: &Address) -> Option<Self> {
        Some(if address == &exit_to_ethereum::ADDRESS {
            Self::EXIT_TO_ETHEREUM
        } else if address == &exit_to_near::ADDRESS {
            Self::EXIT_TO_NEAR
        } else {
            return None;
        })
    }

    /// Checks if the precompile belonging to the `address` is marked as paused.
    #[must_use]
    pub fn is_paused_by_address(&self, address: &Address) -> bool {
        Self::from_address(address).is_some_and(|precompile_flag| self.contains(precompile_flag))
    }
```
