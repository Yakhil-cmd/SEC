### Title
`exitToNear` Precompile Uses `ft_transfer` Instead of `ft_transfer_call` in Legacy Path, Allowing Permanent Freezing of User NEP-141 Tokens in Recipient NEAR Contracts - (`File: engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` uses `"ft_transfer"` (not `"ft_transfer_call"`) in the legacy path for both ETH base-token exits and ERC-20 token exits. When a user specifies a NEAR contract address as the recipient without an Omni message, the NEP-141 tokens are credited to that contract's balance via a plain `ft_transfer`, which does **not** invoke `ft_on_transfer` on the recipient. If the recipient NEAR contract has no mechanism to subsequently call `ft_transfer` outward, the tokens are permanently frozen. This is the direct analog of the `transferFrom` vs. `safeTransferFrom` vulnerability class from the reference report.

---

### Finding Description

In `exit_base_token_to_near` (the ETH base-token path), when `exit_params.message` is `None`, the function returns `"ft_transfer"` as the method: [1](#0-0) 

In `exit_erc20_token_to_near` (the ERC-20 path), the `_` catch-all arm (which fires when no message is provided, including when a user mistakenly appends `:unwrap` to a non-wNEAR token) also returns `"ft_transfer"`: [2](#0-1) 

The selected method string is then used directly to build the outgoing NEAR promise: [3](#0-2) 

`ft_transfer` is a fire-and-forget NEP-141 transfer. It credits the recipient's balance in the NEP-141 contract's storage but does **not** call `ft_on_transfer` on the recipient. The recipient contract is never notified. By contrast, the Omni path correctly uses `"ft_transfer_call"`: [4](#0-3) 

This inconsistency mirrors the mixed `safeTransferFrom`/`transferFrom` usage flagged in the reference report.

The `exit_to_near_precompile_callback` only triggers a refund when the underlying promise **fails**: [5](#0-4) 

If `ft_transfer` **succeeds** (i.e., the recipient contract is registered with the NEP-141 contract and has a storage deposit), no refund path exists. The ERC-20 tokens have already been burned on Aurora, and the NEP-141 tokens now sit in the recipient contract's balance with no recovery mechanism inside the engine.

---

### Impact Explanation

**Permanent freezing of funds.** The attacker-controlled or user-specified `receiver_account_id` is a NEAR contract that:
1. Has a storage deposit with the relevant NEP-141 contract (so `ft_transfer` succeeds), and
2. Does not expose any method that calls `ft_transfer` or `ft_transfer_call` outward (e.g., a simple escrow, a DeFi vault, or any contract whose token-handling logic is gated entirely on `ft_on_transfer`).

In this scenario, the ERC-20 tokens are irreversibly burned on Aurora, the NEP-141 tokens are credited to the contract's balance, the contract is never notified, and no engine-level refund is triggered. The user's funds are permanently lost.

---

### Likelihood Explanation

**Medium.** The entry path is fully unprivileged: any EVM user can call the `exitToNear` precompile by sending a transaction to address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` with flag `0x00` (ETH) or `0x01` (ERC-20) and a plain NEAR account ID as the recipient (no `:` separator). NEAR account IDs do not syntactically distinguish contracts from EOAs, so a user may unknowingly target a contract. DeFi protocols on NEAR routinely hold storage deposits with bridged NEP-141 tokens, making the precondition realistic. The only mitigating factor is that the user must omit the Omni message; however, the legacy path is the default and is the path documented for simple withdrawals.

---

### Recommendation

1. **Preferred fix**: Replace `"ft_transfer"` with `"ft_transfer_call"` in both legacy arms, supplying an empty `msg`. Under NEP-141, if the recipient does not implement `ft_on_transfer`, the call fails and the NEP-141 contract refunds the tokens to the sender (Aurora). The `exit_to_near_precompile_callback` already handles the failure case and can trigger an EVM-level refund when `error_refund` is enabled.

2. **Alternative**: Validate that the recipient account ID is not a known contract (e.g., by checking whether it ends in `.near` sub-account patterns associated with deployed contracts), and reject the legacy path for such recipients.

3. **Minimum**: Add explicit on-chain documentation (via a revert message) warning that the legacy path is unsafe for NEAR contract recipients and directing users to the Omni path.

---

### Proof of Concept

1. Deploy a NEAR contract `vault.near` that calls `storage_deposit` on `usdt.tether-token.near` (so it is registered) but exposes no method that calls `ft_transfer` or `ft_transfer_call`.
2. Bridge USDT to Aurora; user holds ERC-20 USDT on Aurora.
3. User calls the `exitToNear` precompile with input: `[0x01 || amount_u256_be || b"vault.near"]` (no `:` separator, so `message` is `None`).
4. `exit_erc20_token_to_near` selects the `_` arm, returns method `"ft_transfer"`.
5. The ERC-20 tokens are burned on Aurora; a `ft_transfer` promise is dispatched to `usdt.tether-token.near` crediting `vault.near`.
6. `ft_transfer` succeeds (storage deposit exists); no callback fires.
7. `vault.near` now holds USDT in its NEP-141 balance but was never notified and has no outbound transfer function.
8. Tokens are permanently frozen; the user's ERC-20 balance on Aurora is zero with no recovery path.

### Citations

**File:** engine-precompiles/src/native.rs (L456-468)
```rust
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
```

**File:** engine-precompiles/src/native.rs (L536-553)
```rust
        None => Ok((
            eth_connector_account_id,
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            format!(
                r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                exit_params.receiver_account_id,
                context.apparent_value.as_u128()
            ),
            events::ExitToNear::Legacy(ExitToNearLegacy {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
            }),
            "ft_transfer".to_string(),
            None,
        )),
```

**File:** engine-precompiles/src/native.rs (L610-623)
```rust
        // In this flow, we're just forwarding the `msg` to the `ft_transfer_call` transaction.
        Some(Message::Omni(msg)) => (
            nep141_account_id,
            ft_transfer_call_args(&exit_params.receiver_account_id, exit_params.amount, msg)?,
            "ft_transfer_call",
            None,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(erc20_address),
                erc20_address: Address::new(erc20_address),
                dest: exit_params.receiver_account_id.to_string(),
                amount: exit_params.amount,
                msg: msg.to_string(),
            }),
        ),
```

**File:** engine-precompiles/src/native.rs (L624-646)
```rust
        // The legacy flow. Just withdraw the tokens to the NEAR account id.
        // P.S. We use underscore here instead of `None` to handle the case when a user
        // could add the `unwrap` suffix for non wNEAR ERC-20 token by mistake.
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
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
