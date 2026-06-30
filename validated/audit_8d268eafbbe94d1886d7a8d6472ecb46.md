### Title
Permanent Fund Loss When `ft_transfer` Fails in `ExitToNear` Precompile Without `error_refund` Feature - (`engine-precompiles/src/native.rs`)

### Summary

When the Aurora Engine is compiled without the `error_refund` feature (which is **not** in the default feature set), a user who calls the `ExitToNear` precompile to bridge ERC-20 tokens or ETH from the Aurora EVM to NEAR will have their EVM-side funds permanently destroyed if the NEAR-side `ft_transfer` promise fails. No callback is scheduled to handle the failure, and no recovery mechanism exists.

### Finding Description

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) handles bridging of ERC-20 tokens and ETH from the Aurora EVM to NEAR. The flow is:

1. For ERC-20 exits: the ERC-20 contract burns the user's tokens and calls the precompile.
2. For ETH exits: the ETH is deducted from the user's EVM balance and sent to the precompile address.
3. A NEAR promise is scheduled to call `ft_transfer` (or `ft_transfer_call`) on the NEP-141 contract.

The precompile constructs `ExitToNearPrecompileCallbackArgs` to decide whether to attach a callback: [1](#0-0) 

When `error_refund` is **not** enabled, `refund` is hardcoded to `None`. For regular ERC-20 `ft_transfer` and ETH `ft_transfer` exits, `transfer_near_args` is also `None` (it is only `Some` for the wNEAR unwrap path): [2](#0-1) 

When both fields are `None`, `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()`, so the promise is created as `PromiseArgs::Create` — **with no callback at all**. If the `ft_transfer` promise fails on the NEAR side, there is no handler to refund the user's burned ERC-20 tokens or lost ETH.

The `error_refund` feature is **not** in the default feature set: [3](#0-2) 

The feature is defined as an empty flag in the precompiles crate: [4](#0-3) 

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

- For ERC-20 exits: the ERC-20 tokens are burned from the user's EVM balance before the NEAR promise is dispatched. If `ft_transfer` fails (e.g., recipient not registered with the NEP-141 token, NEP-141 contract paused, storage deposit insufficient), the tokens are gone permanently — burned on the EVM side, never credited on the NEAR side.
- For ETH exits: the ETH is deducted from the user's EVM balance and transferred to the precompile address. If `ft_transfer` fails, the ETH is permanently locked in the precompile address with no recovery path.

The engine's own tests confirm this behavior explicitly: [5](#0-4) [6](#0-5) 

### Likelihood Explanation

**Medium.** The `ft_transfer` promise can fail for several realistic, user-triggerable reasons:

- The recipient NEAR account is not registered (has no storage deposit) with the NEP-141 token contract — a common situation for new accounts.
- The NEP-141 contract is paused.
- The recipient account does not exist on NEAR.

Any user who provides a valid but unregistered NEAR recipient account ID will trigger this path and permanently lose their funds. This requires no special privileges — any EVM user can call the `ExitToNear` precompile.

### Recommendation

Enable the `error_refund` feature in the production build of the engine. When enabled, the precompile includes a 20-byte `refund_address` in the input and populates `RefundCallArgs`, which causes `exit_to_near_precompile_callback` to be scheduled as a callback. On failure, `engine::refund_on_error` re-mints burned ERC-20 tokens or returns ETH to the refund address: [7](#0-6) [8](#0-7) 

If the feature cannot be enabled for all deployments, a fallback recovery mechanism (e.g., an admin-callable rescue function for funds stranded at the precompile address) should be added.

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature (the default).
2. Bridge a NEP-141 token to Aurora EVM, receiving ERC-20 tokens.
3. Call the `ExitToNear` precompile with a recipient NEAR account that is **not registered** with the NEP-141 token (no storage deposit).
4. The ERC-20 tokens are burned from the user's EVM balance.
5. The `ft_transfer` promise fails because the recipient is unregistered.
6. No callback fires. No refund occurs.
7. The user's ERC-20 tokens are permanently destroyed; the NEP-141 tokens remain locked in the Aurora engine contract with no way to recover them.

The `ExitToNear` precompile address is `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`: [9](#0-8)

### Citations

**File:** engine-precompiles/src/native.rs (L270-278)
```rust
pub mod exit_to_near {
    use crate::prelude::types::{Address, make_address};

    /// Exit to NEAR precompile address
    ///
    /// Address: `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`
    /// This address is computed as: `&keccak("exitToNear")[12..]`
    pub const ADDRESS: Address = make_address(0xe9217bc7, 0x0b7ed1f598ddd3199e80b093fa71124f);
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

**File:** engine-precompiles/src/native.rs (L470-483)
```rust
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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
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

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
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

**File:** engine/src/engine.rs (L1176-1224)
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
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```
