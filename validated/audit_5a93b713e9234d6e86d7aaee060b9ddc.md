### Title
`ExitToNear` Precompile Returns EVM Success Before NEAR Promise Executes, Causing Permanent Token Loss Without `error_refund` Feature - (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile always returns `Ok(PrecompileOutput {...})` to the EVM — burning the user's ERC-20 tokens and marking the transaction as `TransactionStatus::Succeed` — before the asynchronous NEAR-side `ft_transfer` promise executes. When the NEAR promise fails (e.g., recipient not registered with the NEP-141 contract), and the `error_refund` compile-time feature is absent (which it is by default in production), no refund is issued. The user's tokens are permanently destroyed with no corresponding NEAR-side credit, while the EVM transaction receipt shows success.

---

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` handles ERC-20 → NEAR token bridge exits. Its `run()` method:

1. Validates input and parses the recipient NEAR account ID.
2. Constructs a `PromiseCreateArgs` targeting the NEP-141 contract's `ft_transfer` or `ft_transfer_call` method.
3. Returns `Ok(PrecompileOutput { logs: vec![promise_log, exit_event_log], ... })` — **success** — to the EVM. [1](#0-0) 

The promise is not executed inline. It is encoded as a log and later extracted by `filter_promises_from_logs` in `engine/src/engine.rs`, which schedules it as an asynchronous NEAR receipt after the EVM transaction commits. [2](#0-1) 

The ERC-20 burn and EVM state changes are applied before the NEAR promise runs. If the NEAR `ft_transfer` promise fails, the callback `exit_to_near_precompile_callback` is invoked. Its refund branch is gated on `args.refund`: [3](#0-2) 

The `refund` field is populated only when the `error_refund` compile-time feature is enabled: [4](#0-3) 

The `error_refund` feature is **not** part of the `default` or `contract` feature sets: [5](#0-4) 

When `error_refund` is absent, `args.refund` is always `None`. A failed NEAR promise falls into the `else { None }` branch — no refund, no error, no on-chain record of failure visible to the EVM caller.

---

### Impact Explanation

**Impact: Critical — Permanent loss of user funds.**

When a user calls `withdrawToNear` on a bridged ERC-20 contract with a recipient NEAR account that is not registered with the NEP-141 token (or does not exist), the following occurs:

- ERC-20 tokens are burned from the user's EVM balance (irreversible within the EVM).
- The EVM transaction returns `TransactionStatus::Succeed`.
- The NEAR `ft_transfer` promise fails silently.
- Without `error_refund`, the callback issues no refund.
- The tokens are permanently destroyed — neither present in the EVM nor credited on NEAR.

This is confirmed by the existing test, which explicitly documents the no-refund behavior when the feature is absent: [6](#0-5) 

---

### Likelihood Explanation

**Likelihood: High.**

- Any EVM user holding bridged ERC-20 tokens can trigger this by specifying a NEAR recipient that is not registered with the NEP-141 contract. NEAR's NEP-141 standard requires explicit `storage_deposit` registration before an account can receive tokens; unregistered recipients are common.
- The `error_refund` feature is not enabled in the default or `contract` feature sets, meaning production deployments compiled without explicitly opting in are vulnerable.
- No special privileges are required. The attacker-controlled entry path is the standard `withdrawToNear(bytes, uint256)` call on any bridged ERC-20 contract.
- The EVM transaction receipt shows success, so users and monitoring tools receive no on-chain signal of failure.

---

### Recommendation

**Short Term:** Enable the `error_refund` feature in the production `contract` feature set so that failed NEAR-side promises always trigger a refund of burned ERC-20 tokens.

**Long Term:** Decouple the EVM-side token burn from the NEAR-side promise scheduling. The burn should only be finalized after the NEAR promise succeeds (i.e., use a lock/escrow pattern rather than an immediate burn). Additionally, document clearly that the EVM transaction success does not guarantee NEAR-side delivery.

---

### Proof of Concept

1. Alice holds 1000 units of a bridged ERC-20 token on Aurora.
2. Alice calls `withdrawToNear(bytes("unregistered.near"), 1000)` on the ERC-20 contract.
3. The ERC-20 contract calls the `ExitToNear` precompile (flag `0x1`).
4. The precompile burns 1000 tokens from Alice's EVM balance and returns `Ok(PrecompileOutput {...})`.
5. `filter_promises_from_logs` schedules a NEAR `ft_transfer` promise to `unregistered.near`.
6. The EVM transaction receipt shows `TransactionStatus::Succeed`.
7. The NEAR `ft_transfer` fails because `unregistered.near` has no storage deposit with the NEP-141 contract.
8. `exit_to_near_precompile_callback` is called; `args.refund` is `None` (no `error_refund` feature).
9. The `else { None }` branch executes — no refund.
10. Alice's 1000 ERC-20 tokens are permanently destroyed. She receives nothing on NEAR.

Root cause lines: [4](#0-3) [7](#0-6) [8](#0-7)

### Citations

**File:** engine-precompiles/src/native.rs (L449-454)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
```

**File:** engine-precompiles/src/native.rs (L462-501)
```rust
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
        let promise_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: Vec::new(),
            data: borsh::to_vec(&promise).unwrap(),
        };
        let ethabi::RawLog { topics, data } = exit_event.encode();
        let exit_event_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: topics.into_iter().map(|h| H256::from(h.0)).collect(),
            data,
        };

        Ok(PrecompileOutput {
            logs: vec![promise_log, exit_event_log],
            cost: required_gas,
            output: Vec::new(),
        })
    }
```

**File:** engine/src/engine.rs (L1648-1685)
```rust
            if log.address == exit_to_near::ADDRESS.raw()
                || log.address == exit_to_ethereum::ADDRESS.raw()
            {
                if log.topics.is_empty() {
                    if let Ok(promise) = PromiseArgs::try_from_slice(&log.data) {
                        match promise {
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
                            }
                            PromiseArgs::Callback(promise) => {
                                // Safety: This is safe because the promise data comes from our own
                                // exit precompiles. See note above.
                                let base_id = match previous_promise {
                                    Some(base_id) => {
                                        schedule_promise_callback(handler, base_id, &promise.base)
                                    }
                                    None => schedule_promise(handler, &promise.base),
                                };
                                let id =
                                    schedule_promise_callback(handler, base_id, &promise.callback);
                                previous_promise = Some(id);
                            }
                            PromiseArgs::Recursive(_) => {
                                unreachable!("Exit precompiles do not produce recursive promises")
                            }
                        }
                    }
                    // do not pass on these "internal logs" to the caller
                    None
```

**File:** engine/src/contract_methods/connector.rs (L214-242)
```rust
        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };
```

**File:** engine/Cargo.toml (L42-49)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
integration-test = ["log"]
```

**File:** engine-tests/src/tests/erc20_connector.rs (L656-665)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
```
