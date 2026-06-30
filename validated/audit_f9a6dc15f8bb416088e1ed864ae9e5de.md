### Title
Missing Error-Handling Callback on NEAR Transfer in wNEAR Unwrap Path Causes Permanent Fund Loss - (`engine/src/contract_methods/connector.rs`)

---

### Summary

In `exit_to_near_precompile_callback`, when a user unwraps wNEAR ERC-20 tokens to native NEAR, the function creates a second NEAR transfer promise (`PromiseBatchAction::Transfer`) with **no error-handling callback**. If this transfer fails (e.g., the recipient account does not exist on NEAR), the user's wNEAR ERC-20 tokens are already burned and the unwrapped NEAR is stranded in Aurora's account with no refund path. This is a direct analog to the external report's "best-effort mechanism that does not guarantee coverage of the outstanding obligation."

---

### Finding Description

The wNEAR unwrap flow in the `ExitToNear` precompile proceeds as follows:

**Step 1 – ERC-20 burn and promise construction** (`engine-precompiles/src/native.rs`):

When the user calls `exit_to_near` with the `:unwrap` suffix on the wNEAR ERC-20, `exit_erc20_token_to_near` sets `method = "near_withdraw"` and populates `transfer_near_args`: [1](#0-0) 

The precompile then constructs a two-step promise: `near_withdraw` on the wNEAR contract, followed by a callback to `exit_to_near_precompile_callback`: [2](#0-1) 

**Step 2 – Callback handles `near_withdraw` result** (`engine/src/contract_methods/connector.rs`):

Inside `exit_to_near_precompile_callback`, if `near_withdraw` succeeded, a new `PromiseBatchAction::Transfer` is created and returned — **with no callback attached**: [3](#0-2) 

If `near_withdraw` failed, the `refund` branch re-mints the ERC-20 tokens (when the `error_refund` feature is compiled in): [4](#0-3) 

**The gap**: There is no third-level callback to handle failure of the NEAR transfer promise. If `near_withdraw` succeeds (NEAR lands in Aurora's account) but the subsequent `Transfer` action fails (e.g., the recipient named account does not exist), the protocol has:

- Burned the user's wNEAR ERC-20 tokens (irreversible EVM state change)
- Unwrapped the wNEAR to NEAR in Aurora's account (NEAR is now stranded there)
- No mechanism to re-mint the ERC-20 tokens or return the NEAR to the user

The `refund` branch is only reachable when `near_withdraw` itself fails — it is never triggered by a failure of the downstream NEAR transfer.

---

### Impact Explanation

**Permanent freezing of user funds (Critical).**

The user's wNEAR ERC-20 balance is burned at EVM execution time. The NEAR unwrapped from wNEAR accumulates in Aurora's contract account with no on-chain recovery path for the affected user. There is no admin function that automatically redistributes stranded NEAR back to the original sender.

---

### Likelihood Explanation

**Medium.** The failure condition is a NEAR `Transfer` action to a non-existent named account. This is reachable by:

1. A user who makes a typo in the recipient account ID (the `parse_recipient` function validates only syntactic correctness, not account existence).
2. A user who specifies an account that is deleted on NEAR between the time the EVM transaction is submitted and the time the NEAR transfer receipt executes (NEAR's asynchronous execution model makes this a real race window).

The wNEAR unwrap path is a production feature actively used on Aurora mainnet. [5](#0-4) 

---

### Recommendation

Attach a third-level error-handling callback to the `PromiseBatchAction::Transfer` promise created inside `exit_to_near_precompile_callback`. If the NEAR transfer fails, this callback should invoke `refund_on_error` to re-mint the user's wNEAR ERC-20 tokens, mirroring the existing refund logic used when `near_withdraw` itself fails.

Alternatively, validate that the recipient account exists on NEAR before initiating the exit (e.g., via a preflight `account_exists` check), and revert the EVM transaction if it does not.

---

### Proof of Concept

1. Deploy wNEAR and bridge it to Aurora so a user holds wNEAR ERC-20 tokens.
2. The user calls `exit_to_near` with input flag `0x1` (ERC-20 exit), the wNEAR ERC-20 address as caller, and recipient `nonexistent_account.near:unwrap`.
3. The EVM burns the user's wNEAR ERC-20 tokens and emits a promise log.
4. Aurora executes `near_withdraw` on the wNEAR contract — this succeeds, crediting Aurora's account with native NEAR.
5. `exit_to_near_precompile_callback` fires; the `near_withdraw` result is `Successful`, so the code path at line 215–228 of `connector.rs` executes and schedules `PromiseBatchAction::Transfer` to `nonexistent_account.near`.
6. The NEAR transfer receipt fails because `nonexistent_account.near` does not exist.
7. No further callback fires. The user's wNEAR ERC-20 tokens remain burned; the NEAR is stranded in Aurora's account. [6](#0-5) [7](#0-6)

### Citations

**File:** engine-precompiles/src/native.rs (L359-378)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
        || (recipient, None),
        |(recipient, msg)| {
            if msg == UNWRAP_WNEAR_MSG {
                (recipient, Some(Message::UnwrapWnear))
            } else {
                (recipient, Some(Message::Omni(msg)))
            }
        },
    );

    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
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

**File:** engine-precompiles/src/native.rs (L585-609)
```rust
    let (nep141_account_id, args, method, transfer_near_args, event) = match exit_params.message {
        // wNEAR address should be set via the `factory_set_wnear_address` transaction first.
        Some(Message::UnwrapWnear) if erc20_address == get_wnear_address(io).raw() =>
        // The flow is following here:
        // 1. We call `near_withdraw` on wNEAR account id on `aurora` behalf.
        // In such way we unwrap wNEAR to NEAR.
        // 2. After that, we call callback `exit_to_near_precompile_callback` on the `aurora`
        // in which make transfer of unwrapped NEAR to the `target_account_id`.
        {
            (
                nep141_account_id,
                format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
                "near_withdraw",
                Some(TransferNearArgs {
                    target_account_id: exit_params.receiver_account_id.clone(),
                    amount: exit_params.amount.as_u128(),
                }),
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```

**File:** engine/src/contract_methods/connector.rs (L214-230)
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
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```
