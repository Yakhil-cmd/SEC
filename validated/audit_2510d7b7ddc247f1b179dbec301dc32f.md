### Title
Rebasing NEP-141 Token Bridge Accounting Invariant Broken — Permanent Fund Freeze or Loss - (`engine/src/engine.rs`, `engine-precompiles/src/native.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

The Aurora Engine NEP-141 ↔ ERC-20 bridge maintains a strict 1:1 invariant: the ERC-20 total supply on the EVM side must equal the NEP-141 balance held by the Aurora contract. This invariant is silently violated by any rebasing NEP-141 token (one whose balance changes outside of transfers). A positive rebase permanently freezes the surplus NEP-141 tokens inside the Aurora contract with no ERC-20 tokens to claim them. A negative rebase causes the last withdrawers to find the Aurora contract insolvent, resulting in permanent loss of their ERC-20 value.

---

### Finding Description

**Deposit path** (`ft_on_transfer` → `receive_erc20_tokens`):

When a user sends NEP-141 tokens to Aurora via `ft_transfer_call`, the engine mints exactly `args.amount` ERC-20 tokens to the recipient. [1](#0-0) [2](#0-1) 

The minted amount is fixed at the value reported by the NEP-141 contract at transfer time. No snapshot of the Aurora contract's actual NEP-141 balance is taken, and no mechanism exists to reconcile future balance changes.

**Withdrawal path** (`withdrawToNear` → `ExitToNear` precompile → `ft_transfer`):

When a user withdraws, `EvmErc20.sol` burns exactly `amount` ERC-20 tokens and passes that same `amount` to the `ExitToNear` precompile: [3](#0-2) [4](#0-3) 

The precompile then calls `ft_transfer` on the NEP-141 contract with that same fixed `amount`: [5](#0-4) 

There is no step that queries the actual NEP-141 balance held by Aurora or adjusts the withdrawal amount proportionally. The entire bridge assumes the NEP-141 balance held by Aurora equals the ERC-20 total supply at all times.

---

### Impact Explanation

**Positive rebase (permanent fund freeze):**

1. User deposits 100 rebasing NEP-141 tokens → Aurora holds 100 NEP-141, mints 100 ERC-20.
2. Positive rebase occurs → Aurora now holds 110 NEP-141 (10 extra credited by the token contract).
3. User withdraws 100 ERC-20 → burns 100 ERC-20 → Aurora sends 100 NEP-141 back.
4. The extra 10 NEP-141 tokens are permanently locked inside the Aurora contract. No ERC-20 tokens exist to claim them, and no recovery mechanism exists.

**Negative rebase (insolvency / permanent fund loss):**

1. User A deposits 100 rebasing NEP-141 tokens → Aurora holds 100 NEP-141, mints 100 ERC-20.
2. Negative rebase occurs → Aurora now holds 90 NEP-141.
3. User A withdraws 90 ERC-20 → succeeds (Aurora sends 90 NEP-141).
4. User B tries to withdraw 10 ERC-20 → burns 10 ERC-20 → Aurora calls `ft_transfer` for 10 NEP-141 but holds 0 → `ft_transfer` fails.
   - With `error_refund` feature enabled: ERC-20 tokens are re-minted to the user (temporary freeze, but the underlying NEP-141 is still gone).
   - Without `error_refund`: ERC-20 tokens are permanently burned with no NEP-141 received — direct fund loss. [6](#0-5) 

---

### Likelihood Explanation

Any NEP-141 token whose balance changes outside of transfers (e.g., staking-reward tokens, elastic-supply tokens) triggers this bug automatically upon bridging. The entry path requires no special privilege: any token holder can call `ft_transfer_call` on the NEP-141 contract to bridge tokens to Aurora. The rebase itself is the token's normal operation. No attacker action is required beyond the initial deposit; the accounting divergence is automatic and irreversible.

---

### Recommendation

1. **Snapshot-based accounting**: Record the Aurora contract's actual NEP-141 balance before and after each `ft_on_transfer`, and mint ERC-20 tokens equal to the balance delta rather than `args.amount`. Similarly, on withdrawal, transfer the proportional share of the actual NEP-141 balance rather than the face value of burned ERC-20 tokens.

2. **Alternatively, document and enforce exclusion**: If rebasing NEP-141 tokens are out of scope, add an on-chain check (e.g., a registry of approved token types) that rejects `ft_on_transfer` calls from tokens known to rebase, preventing the invariant from ever being violated.

---

### Proof of Concept

```
1. Deploy a rebasing NEP-141 token `rebase.near` that increases all holder balances by 10% on each `rebase()` call.

2. Alice calls:
     rebase.near::ft_transfer_call(
       receiver_id: "aurora",
       amount: "1000",
       msg: "<alice_evm_address>"
     )
   → Aurora's ft_on_transfer fires → receive_erc20_tokens mints 1000 ERC-20 to Alice.
   → Aurora holds 1000 rebase.near tokens.

3. Token admin calls rebase.near::rebase().
   → Aurora's balance of rebase.near is now 1100 (positive rebase).

4. Alice calls EvmErc20::withdrawToNear("alice.near", 1000).
   → Burns 1000 ERC-20.
   → ExitToNear precompile calls ft_transfer(receiver_id: "alice.near", amount: "1000").
   → Alice receives 1000 rebase.near tokens.
   → Aurora still holds 100 rebase.near tokens with zero ERC-20 supply to claim them.
   → Those 100 tokens are permanently frozen.

5. For the negative rebase case: after step 3 with a 10% negative rebase,
   Aurora holds 900 tokens. Alice tries to withdraw 1000 ERC-20 → ft_transfer
   for 1000 fails (Aurora only has 900). Alice's 1000 ERC-20 are burned
   (without error_refund) or temporarily stuck (with error_refund), but the
   100-token shortfall is unrecoverable.
``` [7](#0-6) [8](#0-7) [3](#0-2)

### Citations

**File:** engine/src/engine.rs (L796-844)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;

        sdk::log!("Mint {amount} ERC-20 tokens for: {}", recipient.encode());

        // Return SubmitResult so that it can be accessed in standalone engine.
        // This is used to help with the indexing of bridge transactions.
        Ok(Some(result))
    }
```

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L195-245)
```rust
#[named]
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

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

        Ok(maybe_result)
    })
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L558-656)
```rust
fn exit_erc20_token_to_near<I: IO>(
    context: &Context,
    exit_params: &Erc20TokenParams,
    io: &I,
) -> Result<
    (
        AccountId,
        String,
        events::ExitToNear,
        String,
        Option<TransferNearArgs>,
    ),
    ExitError,
> {
    // In case of withdrawing ERC-20 tokens, the `apparent_value` should be zero. In opposite way
    // the funds will be locked in the address of the precompile without any possibility
    // to withdraw them in the future. So, in case if the `apparent_value` is not zero, the error
    // will be returned to prevent that.
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }

    let erc20_address = context.caller; // because ERC-20 contract calls the precompile.
    let nep141_account_id = get_nep141_from_erc20(erc20_address.as_bytes(), io)?;

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
    };

    Ok((
        nep141_account_id,
        args,
        event,
        method.to_string(),
        transfer_near_args,
    ))
}
```
