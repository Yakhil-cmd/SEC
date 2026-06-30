### Title
NEP-141 Fee-on-Transfer Token Accounting Mismatch Causes Permanent ERC-20 Fund Freeze - (File: `engine/src/contract_methods/connector.rs`, `engine-precompiles/src/native.rs`)

---

### Summary

The Aurora Engine's NEP-141 → ERC-20 bridge mints mirror tokens based on the `amount` field reported in `ft_on_transfer`, without verifying the actual NEP-141 balance increase received by the engine. For fee-on-transfer or deflationary NEP-141 tokens, the engine mints more ERC-20 tokens than the NEP-141 backing it holds. When a user later exits via the `ExitToNear` precompile, the `ft_transfer` call on the NEP-141 contract fails because Aurora Engine holds fewer tokens than requested. Without the `error_refund` feature active, no refund callback is scheduled, so the ERC-20 tokens are permanently burned with no NEP-141 returned — a permanent fund freeze.

---

### Finding Description

**Deposit path — over-minting:**

In `ft_on_transfer`, the engine dispatches to `receive_erc20_tokens` using `args.amount` directly from the NEP-141 callback: [1](#0-0) 

The `args.amount` is the amount the *caller* specified in `ft_transfer_call`, not the amount Aurora Engine's account balance actually increased by. For a fee-on-transfer NEP-141 token (e.g., one that deducts 5% on every transfer), Aurora Engine's NEP-141 balance increases by `amount - fee`, but the engine mints `amount` ERC-20 tokens to the depositor. This creates an immediate accounting deficit: the ERC-20 supply exceeds the NEP-141 backing.

**Exit path — under-funded transfer with no refund:**

When the user exits via the `ExitToNear` precompile, `exit_erc20_token_to_near` constructs an `ft_transfer` call using the full burned ERC-20 amount: [2](#0-1) 

The `ft_transfer` targets the NEP-141 contract for `exit_params.amount` tokens. Since Aurora Engine only holds `amount - fee` NEP-141 tokens, this call fails on-chain.

**No refund callback for standard ERC-20 exits:**

The promise construction logic only attaches a callback (which would trigger `exit_to_near_precompile_callback` and potentially `refund_on_error`) when `callback_args != default()`. For a standard ERC-20 exit (no wNEAR unwrap, no `error_refund` feature), both `refund` and `transfer_near` are `None`, so `callback_args == default()` and the promise is a bare `PromiseArgs::Create` with no callback: [3](#0-2) 

The ERC-20 tokens are burned atomically in the EVM. The NEP-141 `ft_transfer` then fails asynchronously. With no callback, there is no re-mint of ERC-20 tokens and no NEP-141 transfer. The user's funds are permanently destroyed.

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

A user who deposits a fee-on-transfer NEP-141 token receives more ERC-20 tokens than the protocol can honor. When they attempt to withdraw the full ERC-20 balance:
1. ERC-20 tokens are burned in the EVM (irreversible within the transaction).
2. The `ft_transfer` on the NEP-141 contract fails because Aurora Engine's balance is insufficient.
3. No refund callback is scheduled for standard ERC-20 exits.
4. The user's ERC-20 tokens are gone and no NEP-141 tokens are received.

Additionally, even if the first depositor withdraws only `amount - fee` (the actual backing), subsequent depositors who received over-minted ERC-20 tokens face insolvency: the NEP-141 reserve is exhausted before all ERC-20 holders can exit.

---

### Likelihood Explanation

**Medium-High.** The `deploy_erc20_token` entrypoint imposes no restrictions on which NEP-141 token can be mirrored: [4](#0-3) 

Any unprivileged user can deploy a mirror for any NEP-141 token, including fee-on-transfer tokens. NEAR has several such tokens in production. A user interacting with such a token through the bridge — even innocently — triggers the accounting mismatch. The only mitigation currently mentioned in the codebase is a comment referencing a future admin access list (`// TODO: Add an admin access list`): [5](#0-4) 

No such allowlist is enforced at the time of deposit.

---

### Recommendation

1. **Balance-check on deposit:** In `receive_erc20_tokens`, record Aurora Engine's NEP-141 balance before and after the transfer (via a cross-contract view call or by trusting only the delta), and mint ERC-20 tokens equal to the actual balance increase, not `args.amount`.
2. **Token allowlist:** Enforce a registry of validated NEP-141 tokens before allowing `deploy_erc20_token` or accepting deposits via `ft_on_transfer`. The existing TODO at line 462 of `connector.rs` acknowledges this gap.
3. **Unconditional refund callback:** Ensure the `error_refund` feature is always active in production, or restructure the exit promise to always attach a callback that re-mints ERC-20 tokens if the NEP-141 transfer fails.

---

### Proof of Concept

1. Deploy a NEP-141 token `fee_token.near` that deducts 10% on every `ft_transfer` and `ft_transfer_call`.
2. Call `deploy_erc20_token` on Aurora Engine to create a mirror ERC-20 for `fee_token.near`.
3. Call `ft_transfer_call` on `fee_token.near` with `amount = 1000`, `receiver_id = aurora`, `msg = <evm_recipient>`.
4. Aurora Engine's `ft_on_transfer` is called with `args.amount = 1000`. Aurora Engine's actual NEP-141 balance increases by 900 (10% fee taken). The engine mints **1000** ERC-20 tokens to the EVM recipient.
5. The EVM recipient calls the ERC-20's `withdrawToNear` (or equivalent burn + `ExitToNear` precompile call) for 1000 tokens.
6. The EVM burns 1000 ERC-20 tokens. The `ExitToNear` precompile schedules `ft_transfer` of **1000** `fee_token.near` tokens to the recipient.
7. The `ft_transfer` fails: Aurora Engine only holds 900 `fee_token.near` tokens.
8. No callback is attached (standard ERC-20 exit path, `error_refund` not active). The 1000 ERC-20 tokens are permanently destroyed. The user receives nothing. [6](#0-5) [7](#0-6)

### Citations

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

**File:** engine/src/contract_methods/connector.rs (L112-131)
```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
            }
```

**File:** engine/src/contract_methods/connector.rs (L462-463)
```rust
    // TODO: Add an admin access list of accounts allowed to do it.
    require_owner_only(&state, &io.predecessor_account_id())?;
```

**File:** engine-precompiles/src/native.rs (L444-501)
```rust
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };

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
