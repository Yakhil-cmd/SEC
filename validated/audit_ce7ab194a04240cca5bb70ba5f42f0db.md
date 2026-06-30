### Title
Recipient Grief Attack via `ft_on_transfer` Rejection Causes Permanent Fund Freeze in `ExitToNear` Precompile — (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile burns ERC-20 tokens (or locks ETH) on Aurora before dispatching a NEAR promise. When the `error_refund` feature is disabled — which is the default in the production `contract` build — no callback is attached to the outgoing `ft_transfer_call` promise. A malicious recipient NEAR contract can reject the transfer by returning the full amount from `ft_on_transfer`, causing the NEP-141 tokens to be returned to Aurora while the user's ERC-20 tokens (or ETH) are permanently destroyed with no recovery path.

---

### Finding Description

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) supports an Omni exit flow for both ETH (base token, flag `0x0`) and ERC-20 tokens (flag `0x1`). In both cases, the precompile dispatches a `ft_transfer_call` promise to the NEP-141 contract.

The callback decision is made here:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
// ...
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← NO callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
```

For the Omni `ft_transfer_call` path, `transfer_near_args` is `None` and, without `error_refund`, `refund` is also `None`. This makes `callback_args` equal to `Default::default()`, so the promise is created **without any callback**.

The production WASM build uses the `contract` feature:

```toml
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
```

`error_refund` is **not** included in `contract` or `default`:

```toml
default = ["std"]
error_refund = ["aurora-engine-precompiles/error_refund"]
```

This means the production binary has no refund path.

**Attack sequence (ERC-20 Omni exit):**

1. Attacker deploys a malicious NEAR contract whose `ft_on_transfer` conditionally returns the full `amount` (rejection).
2. Victim calls `exit_to_near` precompile (flag `0x1`, Omni message) targeting the attacker's contract.
3. The ERC-20 contract burns the victim's tokens and calls the precompile.
4. Aurora dispatches `ft_transfer_call` to the NEP-141 contract with no callback.
5. NEP-141 calls `ft_on_transfer` on the attacker's contract; attacker rejects.
6. NEP-141 refunds the tokens back to Aurora's account via `ft_resolve_transfer`.
7. No callback fires on Aurora. The ERC-20 tokens are permanently burned. The NEP-141 tokens sit in Aurora's account with no mechanism to re-mint ERC-20 tokens for the victim.

The same applies to the ETH base-token Omni exit: ETH is locked in `exit_to_near::ADDRESS` and, without `error_refund`, is never returned.

The test suite explicitly documents this behavior:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the recipient rejects:
- ERC-20 tokens are permanently burned on Aurora (irreversible).
- ETH is permanently locked in `exit_to_near::ADDRESS` (irreversible without `error_refund`).
- NEP-141 tokens return to Aurora's account but are inaccessible to the victim (no re-mint path exists).

The victim suffers a total, unrecoverable loss of the bridged asset.

---

### Likelihood Explanation

**Medium.** The Omni exit flow is a production-facing feature reachable by any EVM user. A malicious NEAR contract that conditionally rejects `ft_on_transfer` is trivial to deploy. The attacker only needs to advertise a plausible-looking NEAR receiver address. Any user who calls `exit_to_near` with an Omni message targeting the attacker's contract is permanently drained. The `error_refund` feature is not enabled in the production `contract` build, so no mitigation is active.

---

### Recommendation

Enable the `error_refund` feature in the production `contract` build, or unconditionally attach the `exit_to_near_precompile_callback` callback for all `ft_transfer_call` exit paths. The callback already contains the correct refund logic in `engine/src/contract_methods/connector.rs`:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
```

This callback should fire unconditionally so that a recipient rejection triggers `refund_on_error`, which re-mints ERC-20 tokens or returns ETH to the original sender.

---

### Proof of Concept

**Root cause — no callback when `error_refund` is absent:** [1](#0-0) 

**`error_refund` is not in the default or `contract` feature set:** [2](#0-1) [3](#0-2) 

**Omni `ft_transfer_call` path for ERC-20 exit (no `transfer_near`, no `refund` → default callback args → no callback):** [4](#0-3) 

**Omni `ft_transfer_call` path for ETH base-token exit (same structure):** [5](#0-4) 

**Test confirming permanent loss without `error_refund`:** [6](#0-5) 

**Refund logic that would fire if the callback existed:** [7](#0-6) 

**`refund_on_error` re-mints ERC-20 tokens or returns ETH:** [8](#0-7)

### Citations

**File:** engine-precompiles/src/native.rs (L449-484)
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
        let promise_log = Log {
```

**File:** engine-precompiles/src/native.rs (L519-535)
```rust
        Some(Message::Omni(msg)) => Ok((
            eth_connector_account_id,
            ft_transfer_call_args(
                &exit_params.receiver_account_id,
                context.apparent_value,
                msg,
            )?,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
                msg: msg.to_string(),
            }),
            "ft_transfer_call".to_string(),
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
