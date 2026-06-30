### Title
ERC-20 Tokens Permanently Burned When `ft_on_transfer` Panics in the Omni Exit Path - (File: `engine-precompiles/src/native.rs`)

### Summary

When a user exits ERC-20 tokens to NEAR using the Omni path (`ft_transfer_call`), the ERC-20 tokens are burned first. If the receiver contract's `ft_on_transfer` panics, NEAR's NEP-141 standard refunds the tokens back to Aurora (the sender), and `ft_resolve_transfer` still resolves as **successful**. The `exit_to_near_precompile_callback` sees `PromiseResult::Successful` and does not trigger an ERC-20 re-mint. The user's ERC-20 tokens are permanently destroyed while the NEP-141 tokens are silently re-absorbed into Aurora's balance with no corresponding ERC-20 minted.

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` supports three exit sub-paths. The Omni sub-path is triggered when the recipient string contains a `:` separator (e.g., `receiver_id:some_message`): [1](#0-0) 

In this branch, `transfer_near_args` is hardcoded to `None` and the method is `"ft_transfer_call"`. After the sub-path resolves, the callback args are assembled: [2](#0-1) 

With the `error_refund` feature enabled, `refund` is `Some(...)`, so `callback_args != default()` and the callback `exit_to_near_precompile_callback` is attached. The callback logic is: [3](#0-2) 

The callback only triggers `refund_on_error` when `PromiseResult::Failed`. However, NEAR's NEP-141 `ft_transfer_call` standard works as follows:

1. NEP-141 transfers tokens from Aurora to receiver.
2. NEP-141 calls `ft_on_transfer` on the receiver contract.
3. If `ft_on_transfer` **panics**, NEP-141 calls `ft_resolve_transfer`, which refunds all tokens back to Aurora and resolves **successfully**.
4. The `ft_transfer_call` promise result seen by `exit_to_near_precompile_callback` is therefore `PromiseResult::Successful`.

Because the callback sees `Successful`, it takes the first branch and returns `None` — no ERC-20 re-mint occurs. The ERC-20 tokens were already burned by `EvmErc20.withdrawToNear`: [4](#0-3) 

The NEP-141 tokens are now silently held by Aurora with no corresponding ERC-20 representation. There is no recovery path.

The existing test `test_exit_to_near_refund` only covers the legacy `ft_transfer` path (where the receiver is unregistered and the promise fails outright), not the Omni `ft_transfer_call` path where the receiver panics inside `ft_on_transfer`: [5](#0-4) 

### Impact Explanation

**Critical — Permanent freezing of funds.** The user's ERC-20 tokens are irreversibly burned on the EVM side. The corresponding NEP-141 tokens are silently returned to Aurora's NEP-141 balance with no ERC-20 minted and no event emitted to signal the failure. There is no admin recovery function. The funds are permanently frozen.

### Likelihood Explanation

**Medium.** The Omni path is a documented, intentional feature reachable by any unprivileged EVM user who calls `withdrawToNear` with a `:` in the recipient string. The receiver contract's `ft_on_transfer` panicking is a realistic scenario: the receiver may not implement the interface, may have a bug, or may intentionally revert. The user has no way to know in advance whether the receiver will panic.

### Recommendation

In `exit_to_near_precompile_callback`, when the base promise is `PromiseResult::Successful` and the method was `ft_transfer_call`, parse the returned refunded-amount from the promise result data. If the refunded amount equals the full exit amount (meaning the receiver rejected all tokens), treat this as a failure and invoke `refund_on_error` to re-mint the burned ERC-20 tokens to the refund address.

Alternatively, always attach the callback for the Omni path and inspect the `PromiseResult::Successful` payload (which is the JSON-encoded `U128` of tokens refunded by `ft_resolve_transfer`) to determine whether a partial or full ERC-20 refund is needed.

### Proof of Concept

1. Deploy a NEP-141 token and bridge it to Aurora as an ERC-20 (`erc20`).
2. Deploy a malicious/broken NEAR contract `bad_receiver.near` whose `ft_on_transfer` panics unconditionally.
3. From an EVM account holding `erc20` tokens, call:
   ```solidity
   erc20.withdrawToNear(
       bytes("bad_receiver.near:some_omni_msg"),
       amount
   );
   ```
   This triggers `EvmErc20._burn(sender, amount)` then calls the `ExitToNear` precompile with flag `0x01` and an Omni message.
4. The precompile emits a `ft_transfer_call` promise targeting the NEP-141 contract with `receiver_id = bad_receiver.near`.
5. NEP-141 calls `bad_receiver.near::ft_on_transfer` → panics → `ft_resolve_transfer` refunds all tokens to Aurora → promise resolves `Successful`.
6. `exit_to_near_precompile_callback` sees `PromiseResult::Successful`, skips the refund branch, returns `None`.
7. Observe: ERC-20 balance of sender is reduced by `amount`; NEP-141 balance of Aurora is unchanged (tokens returned); sender has received nothing. Funds are permanently frozen. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-58)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;
```

**File:** engine-tests/src/tests/erc20_connector.rs (L623-666)
```rust
    #[tokio::test]
    async fn test_exit_to_near_refund() {
        // Deploy Aurora; deploy NEP-141; bridge NEP-141 to ERC-20 on Aurora
        let TestExitToNearContext {
            ft_owner,
            ft_owner_address,
            nep_141,
            erc20,
            aurora,
            ..
        } = test_exit_to_near_common().await.unwrap();

        // Call exit on ERC-20; ft_transfer promise fails; expect refund on Aurora;
        exit_to_near(
            &ft_owner,
            // The ft_transfer will fail because this account is not registered with the NEP-141
            "unregistered.near",
            FT_EXIT_AMOUNT,
            &erc20,
            &aurora,
        )
        .await
        .unwrap();

        assert_eq!(
            nep_141_balance_of(&nep_141, &ft_owner.id()).await,
            FT_TOTAL_SUPPLY - FT_TRANSFER_AMOUNT
        );
        assert_eq!(
            nep_141_balance_of(&nep_141, &aurora.id()).await,
            FT_TRANSFER_AMOUNT
        );

        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
    }
```

**File:** engine/src/engine.rs (L1176-1204)
```rust
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I,
    env: &E,
    state: EngineState,
    args: &RefundCallArgs,
    handler: &mut P,
) -> EngineResult<SubmitResult> {
    let current_account_id = env.current_account_id();
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        let erc20_admin_address = current_address(&current_account_id);
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, erc20_admin_address, current_account_id, io, env);

        let refund_address = args.recipient_address;
        let amount = U256::from_big_endian(&args.amount);
        let input = setup_refund_on_error_input(amount, refund_address);

        engine.call(
            &erc20_admin_address,
            &erc20_address,
            Wei::zero(),
            input,
            u64::MAX,
            Vec::new(),
            Vec::new(),
            handler,
        )
    } else {
```
