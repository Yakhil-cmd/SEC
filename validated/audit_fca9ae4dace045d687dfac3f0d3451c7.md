## Full Code Trace

**Step 1 — Omni path in `exit_erc20_token_to_near`**

When a user exits an ERC-20 with an Omni message (`:some_json_msg` suffix), the function returns `transfer_near_args = None`: [1](#0-0) 

**Step 2 — Callback construction**

`callback_args` is built as: [2](#0-1) 

- **With `error_refund` feature**: `refund = Some(RefundCallArgs { erc20_address: Some(...), ... })`, `transfer_near = None` → `callback_args ≠ default()` → callback IS scheduled.
- **Without `error_refund` feature**: `refund = None`, `transfer_near = None` → `callback_args == default()` → **NO callback scheduled**. [3](#0-2) 

**Step 3 — The callback's success/failure check**

When a callback IS scheduled (with `error_refund`), it checks: [4](#0-3) 

The refund (ERC-20 re-mint) only fires if `handler.promise_result(0)` is **not** `Successful`.

**Step 4 — The critical NEP-141 invariant**

`ft_transfer_call` is a multi-step NEAR promise chain:
1. Transfer tokens to receiver
2. Call `receiver.ft_on_transfer(...)` 
3. Call `self.ft_resolve_transfer(...)` as a callback

If `ft_on_transfer` runs out of gas, `ft_resolve_transfer` is still called (NEAR reserves gas for callbacks). `ft_resolve_transfer` detects the failure, returns all tokens to the sender (Aurora's NEP-141 balance), and **itself succeeds** — returning `0` tokens used.

From Aurora's perspective, `ft_transfer_call` returns `PromiseResult::Successful`. The callback sees `Successful` and takes the success branch — **no ERC-20 refund is triggered**.

**Step 5 — Gas constant** [5](#0-4) 

70 TGas is hardcoded. The NEP-141 contract itself consumes some of this gas before forwarding to `ft_on_transfer`, leaving less than 70 TGas available to the receiver.

**Step 6 — `error_refund` feature status** [6](#0-5) 

`error_refund` is **not** in `default` features. Even if it were enabled, the vulnerability persists because the callback checks `PromiseResult::Successful` — which is exactly what `ft_transfer_call` returns when `ft_on_transfer` fails and `ft_resolve_transfer` returns tokens to Aurora.

**Step 7 — Existing test acknowledges the no-refund case** [7](#0-6) 

The codebase explicitly acknowledges that without `error_refund`, tokens are not refunded. But even with `error_refund`, the Omni+`ft_transfer_call` path is not protected because `ft_transfer_call` succeeds at the NEAR level.

---

### Title
ERC-20 Omni Exit Burns Tokens Without Detecting `ft_on_transfer` Gas Exhaustion — (`engine-precompiles/src/native.rs`)

### Summary
When a user exits an ERC-20 via the Omni path (`exit_erc20_token_to_near` with `Message::Omni`), the ERC-20 is burned and `ft_transfer_call` is scheduled with a hardcoded 70 TGas. If the receiver's `ft_on_transfer` exhausts gas, NEP-141's `ft_resolve_transfer` returns all tokens to Aurora's NEP-141 balance and itself succeeds. Aurora's callback sees `PromiseResult::Successful` and does not trigger an ERC-20 re-mint. The user's ERC-20 is permanently burned while the NEP-141 tokens accumulate in Aurora's balance with no user recourse.

### Finding Description
The root cause is a semantic mismatch between what Aurora treats as `ft_transfer_call` success and what NEP-141 guarantees. NEP-141's `ft_transfer_call` is designed to always complete successfully at the protocol level — even when `ft_on_transfer` fails — by returning tokens to the sender via `ft_resolve_transfer`. Aurora's `exit_to_near_precompile_callback` only triggers an ERC-20 refund when `ft_transfer_call` returns `PromiseResult::Failed`. Since `ft_transfer_call` never returns `Failed` in the OOG-on-`ft_on_transfer` scenario, the refund branch is never reached.

Additionally, the 70 TGas allocation is hardcoded and cannot be adjusted by the user, making any receiver requiring more gas permanently incompatible with the Omni exit path. [8](#0-7) 

### Impact Explanation
- ERC-20 tokens are burned on Aurora (irreversible within the EVM).
- NEP-141 tokens are returned to Aurora's NEP-141 balance (not the user's).
- No callback re-mints the ERC-20.
- The user has no on-chain mechanism to recover either the ERC-20 or the NEP-141 tokens.
- Impact: **High — Theft of unclaimed yield** (tokens returned to Aurora's NEP-141 balance after `ft_transfer_call` gas exhaustion are permanently inaccessible to the user).

### Likelihood Explanation
- Any user targeting a DeFi receiver whose `ft_on_transfer` requires more than ~60–65 TGas (accounting for NEP-141 overhead within the 70 TGas budget) will trigger this.
- The user does not need to be malicious; this is triggered by normal usage with complex receivers.
- The 70 TGas limit is hardcoded and cannot be overridden by the caller.
- Likelihood: **Medium** — requires a receiver with high `ft_on_transfer` gas consumption, which is realistic for complex DeFi protocols.

### Recommendation
1. **Read the return value of `ft_transfer_call`**: NEP-141's `ft_transfer_call` returns the number of tokens actually used. If the returned value is less than the sent amount, tokens were returned to Aurora. The callback should compare the returned amount against the sent amount and re-mint the difference.
2. **Allow caller-specified gas**: Let the user specify the gas to attach to `ft_transfer_call` (with a minimum floor), so complex receivers can be accommodated.
3. **Alternatively**: After `ft_transfer_call`, check Aurora's own NEP-141 balance delta and re-mint ERC-20 tokens for any amount returned.

### Proof of Concept
1. Deploy a NEP-141 token; bridge it to Aurora as an ERC-20.
2. Deploy a NEAR contract as the `ft_on_transfer` receiver that consumes exactly 71 TGas (e.g., via a tight loop).
3. Call the ERC-20's `withdraw` function with input `0x01 || amount || receiver_account_id:{"some":"msg"}`.
4. Observe: ERC-20 balance decreases by `amount`; Aurora's NEP-141 balance increases by `amount`; receiver's NEP-141 balance is 0; user has no ERC-20 and no NEP-141.
5. Assert: `ft_transfer_call` promise result is `Successful` (returned by `ft_resolve_transfer`); `exit_to_near_precompile_callback` takes the success branch; no re-mint occurs.

### Citations

**File:** engine-precompiles/src/native.rs (L55-55)
```rust
    pub(super) const FT_TRANSFER_CALL_GAS: NearGas = NearGas::new(70_000_000_000_000);
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

**File:** engine-precompiles/src/native.rs (L611-623)
```rust
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

**File:** engine/Cargo.toml (L43-48)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
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
