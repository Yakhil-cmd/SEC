### Title
Unguarded `deploy_erc20_token` Allows Registration of Malicious NEP-141 Tokens, Enabling Permanent ERC-20 Fund Freeze - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

Any NEAR account can call `deploy_erc20_token` on the Aurora Engine contract to register an arbitrary NEP-141 token as an ERC-20 mirror. A malicious actor can register a NEP-141 token that contains a blacklist mechanism, attract users into holding the corresponding ERC-20 tokens, then blacklist the Aurora contract address on the NEP-141 side. When users attempt to exit (burn ERC-20 → receive NEP-141 via the `ExitToNear` precompile), the `ft_transfer` call to the malicious NEP-141 fails. Because the `error_refund` feature is **not** in the default feature set, the ERC-20 tokens are permanently burned with no NEP-141 received, constituting a permanent fund freeze.

---

### Finding Description

**Root cause — missing access control on `deploy_erc20_token`:**

The `deploy_erc20_token` function in `engine/src/contract_methods/connector.rs` performs only a liveness check (`require_running`) and no caller-identity check: [1](#0-0) 

Compare this with functions that do enforce ownership, such as `set_eth_connector_contract_account` and `mirror_erc20_token`, which call `require_owner_only`: [2](#0-1) 

The public WASM entry point in `lib.rs` adds no additional guard: [3](#0-2) 

**Compounding factor — `error_refund` is not a default feature:**

When the `ExitToNear` precompile fires, it creates a NEAR promise to call `ft_transfer` on the NEP-141 contract. The callback `exit_to_near_precompile_callback` handles the result: [4](#0-3) 

The refund branch is only populated when the `error_refund` Cargo feature is compiled in: [5](#0-4) 

`error_refund` is absent from the `default` feature set: [6](#0-5) 

Without `error_refund`, `args.refund` is always `None`. A failed `ft_transfer` falls through to the final `else { None }` branch — no refund is issued, and the ERC-20 tokens that were already burned inside the EVM execution are permanently lost.

**Attack flow:**

1. Attacker deploys a malicious NEP-141 token on NEAR (e.g., with a `before_transfer` hook that checks a blacklist, analogous to the `MockBAD` in the report).
2. Attacker calls `deploy_erc20_token` on Aurora with the malicious NEP-141 account ID — succeeds with no access control. [7](#0-6) 

3. Attacker calls `ft_transfer_call` on the malicious NEP-141 → Aurora's `ft_on_transfer` mints ERC-20 tokens for the attacker. [8](#0-7) 

4. Attacker sells/transfers ERC-20 tokens to victims (e.g., via a DEX).
5. Attacker blacklists the Aurora contract address on the malicious NEP-141.
6. Victim calls `withdrawToNear` on the ERC-20 contract → `ExitToNear` precompile burns ERC-20 tokens and schedules `ft_transfer` on the malicious NEP-141. [9](#0-8) 

7. `ft_transfer` reverts (blacklisted). `exit_to_near_precompile_callback` receives a failed promise result, `args.refund` is `None` (no `error_refund` feature), returns `None` — no refund, no transfer. ERC-20 tokens are permanently burned.

---

### Impact Explanation

**Permanent freezing of funds.** Victims' ERC-20 tokens are burned inside the EVM (state change committed before the NEAR promise is dispatched) but the corresponding NEP-141 tokens are never transferred. The NEP-141 tokens remain locked inside the Aurora contract with no recovery path, because every future `ft_transfer` attempt to the malicious NEP-141 will also revert. This matches the "Critical — Permanent freezing of funds" impact tier.

Even if `error_refund` is enabled in a specific deployment, the NEP-141 tokens deposited into Aurora remain permanently locked (Aurora is blacklisted and cannot call `ft_transfer`), making the ERC-20 tokens permanently non-redeemable — still a permanent freeze of the underlying NEP-141 value.

---

### Likelihood Explanation

**Medium.** The attacker must:
- Deploy a malicious NEP-141 contract on NEAR (permissionless, low cost).
- Call `deploy_erc20_token` on Aurora (permissionless, confirmed by code).
- Attract users to hold the ERC-20 tokens (e.g., via a DEX listing, airdrop, or DeFi integration).

No privileged access is required at any step. The `deploy_erc20_token` entry point is publicly callable by any NEAR account, and the NEP-141 deployment is permissionless on NEAR.

---

### Recommendation

1. **Add access control to `deploy_erc20_token`**: Restrict callers to the contract owner or a designated admin role, consistent with how `mirror_erc20_token` and `set_eth_connector_contract_account` are protected.

```rust
// In deploy_erc20_token, before processing args:
require_owner_only(&state::get_state(&io)?, &env.predecessor_account_id())?;
```

2. **Enable `error_refund` by default** or make it unconditional: The refund path in `exit_to_near_precompile_callback` should always be active so that a failed `ft_transfer` re-mints the burned ERC-20 tokens, preventing permanent loss.

3. **Consider an explicit NEP-141 whitelist**: Only allow NEP-141 tokens that have been vetted and approved by the contract owner to be registered as ERC-20 mirrors, analogous to the token whitelist fix recommended in the OpenQ report.

---

### Proof of Concept

```
1. Deploy malicious NEP-141 on NEAR:
   - Implements ft_transfer_call, ft_on_transfer normally
   - Has a `set_blacklist(account_id)` admin function
   - `ft_transfer` reverts if `from == blacklisted_account`

2. Call aurora.deploy_erc20_token(malicious_nep141_account_id)
   → Succeeds (no access control check)
   → ERC-20 mirror deployed at some address on Aurora

3. Call malicious_nep141.ft_transfer_call(
       receiver_id = "aurora",
       amount = 1000,
       msg = <victim_evm_address_hex>
   )
   → Aurora's ft_on_transfer mints 1000 ERC-20 tokens for victim

4. Attacker calls malicious_nep141.set_blacklist("aurora")

5. Victim calls erc20.withdrawToNear(<near_recipient>)
   → ExitToNear precompile burns 1000 ERC-20 tokens (EVM state committed)
   → Promise created: malicious_nep141.ft_transfer(aurora → near_recipient, 1000)
   → Promise FAILS (aurora is blacklisted)

6. exit_to_near_precompile_callback fires:
   → promise_result(0) = Failed
   → args.refund = None  (error_refund feature not enabled)
   → returns Ok(None)
   → No refund, no transfer

Result: 1000 ERC-20 tokens burned, 1000 NEP-141 tokens permanently locked in Aurora.
```

### Citations

**File:** engine/src/contract_methods/connector.rs (L81-108)
```rust
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
```

**File:** engine/src/contract_methods/connector.rs (L112-125)
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

**File:** engine/src/contract_methods/connector.rs (L456-463)
```rust
pub fn mirror_erc20_token<I: IO + Env + Copy, H: PromiseHandler>(
    io: I,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    // TODO: Add an admin access list of accounts allowed to do it.
    require_owner_only(&state, &io.predecessor_account_id())?;
```

**File:** engine/src/lib.rs (L613-621)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn deploy_erc20_token() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::deploy_erc20_token(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine-precompiles/src/native.rs (L449-455)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
```

**File:** engine-precompiles/src/native.rs (L462-483)
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
```

**File:** engine/Cargo.toml (L43-48)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** engine/src/engine.rs (L1339-1374)
```rust
/// Used to bridge NEP-141 tokens from NEAR to Aurora. On Aurora the NEP-141 becomes an ERC-20.
pub fn deploy_erc20_token<I: IO + Copy, E: Env, P: PromiseHandler>(
    nep141: AccountId,
    metadata: Option<Erc20Metadata>,
    io: I,
    env: &E,
    handler: &mut P,
) -> Result<Address, DeployErc20Error> {
    let current_account_id = env.current_account_id();
    let input = setup_deploy_erc20_input(&current_account_id, metadata);
    let mut engine: Engine<_, _> = Engine::new(
        aurora_engine_sdk::types::near_account_to_evm_address(
            env.predecessor_account_id().as_bytes(),
        ),
        current_account_id,
        io,
        env,
    )
    .map_err(DeployErc20Error::State)?;

    let address = match engine.deploy_code_with_input(input, None, handler) {
        Ok(result) => match result.status {
            TransactionStatus::Succeed(ret) => {
                Address::new(H160(ret.as_slice().try_into().unwrap()))
            }
            other => return Err(DeployErc20Error::Failed(other)),
        },
        Err(e) => return Err(DeployErc20Error::Engine(e)),
    };

    sdk::log!("Deployed ERC-20 in Aurora at: {:#?}", address);
    engine
        .register_token(address, nep141)
        .map_err(DeployErc20Error::Register)?;

    Ok(address)
```
