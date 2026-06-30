### Title
Permanent Fund Loss When `ExitToNear` Bridge Promise Fails Without `error_refund` Feature - (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToNear` precompile burns ERC-20 tokens (or deducts ETH) from a user's EVM balance before dispatching a NEAR-side `ft_transfer` promise. The mechanism to refund those burned tokens if the NEAR promise fails is gated behind the compile-time `error_refund` feature flag. When that feature is absent — which is the non-default build configuration — no callback is attached to the outgoing promise, and any failure of the NEAR-side transfer permanently destroys the user's funds with no recovery path.

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` is the Aurora bridge exit path. The EVM-side burn happens first (inside the ERC-20 contract that calls the precompile), and then the precompile schedules a NEAR `ft_transfer` or `ft_transfer_call` promise. The refund logic — which would re-mint the burned tokens if that promise fails — is entirely conditional on the `error_refund` Cargo feature:

```rust
// engine-precompiles/src/native.rs  lines 449-455
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

For a standard ERC-20 exit (the most common case), `transfer_near` is also `None`, so `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()`. The branch that decides whether to attach a callback then resolves to the no-callback path:

```rust
// engine-precompiles/src/native.rs  lines 470-483
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback, no refund
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

The `error_refund` feature is **not** listed in the `default` features of either `engine-precompiles` or `engine`:

```toml
# engine-precompiles/Cargo.toml
[features]
default = ["std"]
error_refund = []
``` [3](#0-2) 

```toml
# engine/Cargo.toml
[features]
default = ["std"]
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [4](#0-3) 

When the feature is absent, the `exit_to_near_precompile_callback` entry point is never scheduled. The callback function itself correctly handles the failure case — but it is simply never called:

```rust
// engine/src/contract_methods/connector.rs  lines 231-239
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
    Some(refund_result)
} else {
    None   // ← reached when refund is None and promise failed
};
``` [5](#0-4) 

The `refund_on_error` function in `engine/src/engine.rs` re-mints ERC-20 tokens (or transfers ETH back from the precompile address) — but it is only reachable when `error_refund` is compiled in. [6](#0-5) 

### Impact Explanation

**Critical — Permanent freezing / destruction of funds.**

When the NEAR-side `ft_transfer` promise fails (e.g., the recipient account is not registered with the NEP-141 contract), the ERC-20 tokens have already been burned on the EVM side. Without the callback, they are never re-minted. The user loses the full exit amount with no recovery path. The same applies to ETH exits: the ETH is moved out of the user's EVM balance into the precompile address, and without the callback it is stranded there permanently.

The test suite explicitly documents this outcome:

```rust
// engine-tests/src/tests/erc20_connector.rs  lines 656-660
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [7](#0-6) 

The same pattern is confirmed for ETH exits: [8](#0-7) 

### Likelihood Explanation

**High.** The trigger condition — a NEAR `ft_transfer` failing because the recipient is not registered with the NEP-141 contract — is a routine user mistake. Any EVM user who calls the exit precompile with an unregistered NEAR account ID will permanently lose their tokens. No special privileges or adversarial setup are required; the user only needs to submit a standard EVM transaction targeting the `ExitToNear` precompile address (`0x...`). The precompile is a publicly documented, intended production interface.

### Recommendation

Make the refund mechanism unconditional rather than gating it behind a compile-time feature flag. The `error_refund` feature should either be included in the `default` feature set for the production `engine` crate, or the `refund_call_args` construction and callback attachment should be performed unconditionally in `ExitToNear::run`. Removing the `#[cfg(not(feature = "error_refund"))] refund: None` branch eliminates the silent fund-loss path entirely.

### Proof of Concept

1. User holds ERC-20 tokens on Aurora that mirror a NEP-141 token.
2. User calls the ERC-20 contract's `withdraw` function targeting an unregistered NEAR account (e.g., `"unregistered.near"`).
3. The ERC-20 contract burns the tokens and calls the `ExitToNear` precompile.
4. Without `error_refund`, `callback_args.refund = None` and `callback_args.transfer_near = None`, so `callback_args == default()`.
5. The precompile emits `PromiseArgs::Create(transfer_promise)` — no callback is attached.
6. `filter_promises_from_logs` in `engine/src/engine.rs` schedules the bare promise. [9](#0-8) 
7. The NEAR runtime executes `ft_transfer`; it fails because the recipient is unregistered.
8. No callback fires. The burned ERC-20 tokens are gone. The NEP-141 balance on NEAR is unchanged. The user's funds are permanently destroyed.

### Citations

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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
```

**File:** engine/Cargo.toml (L42-48)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** engine/src/contract_methods/connector.rs (L231-242)
```rust
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

**File:** engine/src/engine.rs (L1651-1665)
```rust
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
```

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
