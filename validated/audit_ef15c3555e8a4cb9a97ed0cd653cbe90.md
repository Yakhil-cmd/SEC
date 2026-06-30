### Title
Omni `ft_transfer_call` Partial Refunds Are Not Re-minted, Permanently Stranding User Funds — (File: `engine-precompiles/src/native.rs`)

---

### Summary

When a user exits ERC-20 tokens to NEAR using the **Omni path** (`ft_transfer_call` with a message), the ERC-20 tokens are burned upfront. If the NEAR receiver contract returns unused tokens (a standard NEP-141 partial refund), those NEP-141 tokens accumulate in Aurora's NEP-141 balance with no mechanism to re-mint the corresponding ERC-20 tokens for the user. The refunded amount is permanently lost to the user — directly analogous to the external report's pattern of using the minimum/expected amount instead of the actual received amount.

---

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` handles ERC-20 exits via two sub-paths:

1. **Legacy** (`ft_transfer`): simple transfer, no refund possible.
2. **Omni** (`ft_transfer_call`): transfer with an arbitrary message; the receiver's `ft_on_transfer` can return unused tokens.

In the Omni path, `exit_erc20_token_to_near` selects `ft_transfer_call` and sets `transfer_near_args = None`: [1](#0-0) 

Back in the `run` method, the callback args are assembled: [2](#0-1) 

For the Omni path:
- `transfer_near` is `None` (not a wNEAR unwrap).
- `refund` is `None` when the `error_refund` feature is disabled; when enabled it only handles a **complete transfer failure**, not a partial refund.

Because both fields are `None`, `callback_args == ExitToNearPrecompileCallbackArgs::default()`, so the branch at line 470 takes the `PromiseArgs::Create` path — **no callback is attached to the `ft_transfer_call` promise**. [3](#0-2) 

**NEP-141 `ft_transfer_call` semantics** (standard): after the receiver's `ft_on_transfer` executes, the NEP-141 contract refunds the returned (unused) amount back to the caller — here, the Aurora engine's NEP-141 balance. Because no callback exists to read that return value and re-mint ERC-20 tokens, the refunded NEP-141 tokens are permanently stranded in Aurora's balance while the user's ERC-20 tokens were already burned.

This is the exact same accounting error as the external report: the actual amount consumed by the receiver (`X − Y`) differs from the amount burned (`X`), and the difference (`Y`) is never returned to the user.

---

### Impact Explanation

**Critical — Permanent freezing / direct theft of user funds.**

The refunded NEP-141 tokens accumulate in Aurora's NEP-141 balance. There is no EVM-side recovery path: the user's ERC-20 tokens were burned before the cross-chain call, and no re-mint ever occurs for the partial refund. The funds are permanently inaccessible to the user.

---

### Likelihood Explanation

**Medium.** The Omni exit path is a documented, user-facing feature for bridging tokens into NEAR DeFi protocols. Any NEAR DeFi contract that does not consume 100% of the transferred tokens (e.g., a DEX that partially fills an order, a lending protocol that caps deposits) will trigger this refund. This is standard NEP-141 behavior, not an edge case.

---

### Recommendation

Attach a callback to the `ft_transfer_call` promise that:
1. Reads the promise return value (the amount refunded by the receiver, as returned by `ft_transfer_call`).
2. Re-mints that amount of ERC-20 tokens to the original sender's EVM address.

This mirrors the fix applied in the external report: account for the **actual** amount consumed rather than assuming the full amount was used.

---

### Proof of Concept

1. User holds 100 USDC ERC-20 tokens on Aurora (backed 1:1 by 100 USDC NEP-141 in Aurora's custody).
2. User calls `USDC.withdraw(100, "defi.near:some_action")` — the Omni exit path.
3. The ERC-20 contract burns 100 USDC from the user and calls the `ExitToNear` precompile with `amount = 100`, `msg = "some_action"`.
4. The precompile schedules `ft_transfer_call(defi.near, 100, "some_action")` on the USDC NEP-141 contract — **no callback attached**.
5. `defi.near.ft_on_transfer(aurora, 100, "some_action")` executes and returns `30` (unused tokens).
6. The USDC NEP-141 contract refunds 30 tokens to Aurora's NEP-141 balance.
7. No callback fires → 30 USDC NEP-141 tokens are permanently stranded in Aurora's balance.
8. The user has irreversibly lost 30 USDC; their ERC-20 balance was reduced by 100 but only 70 were actually consumed. [4](#0-3) [5](#0-4)

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
