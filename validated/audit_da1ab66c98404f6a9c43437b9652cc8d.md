The code evidence is conclusive. Let me trace the exact path.

### Title
Permanent ERC-20 Token Loss on Failed NEP-141 `ft_transfer` Without `error_refund` Feature — (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

When the Aurora Engine is compiled without the `error_refund` feature flag, a user calling `EvmErc20::withdrawToNear` with a NEAR recipient account not registered with the NEP-141 token will have their ERC-20 tokens permanently burned with no on-chain refund. The burn is unconditional and precedes the NEAR-side promise; without `error_refund`, no callback is ever scheduled, so a failed `ft_transfer` silently discards the tokens.

---

### Finding Description

**Step 1 — Burn before promise, no refund address in input.**

`EvmErc20::withdrawToNear` burns the caller's tokens first, then encodes the precompile call as `\x01 + amount_b (32 bytes) + recipient` — no `refund_address` field is included. [1](#0-0) 

**Step 2 — `error_refund` disabled → `refund: None` hardcoded.**

Inside `ExitToNear::run()`, the `callback_args` struct is built with `refund: None` at compile time when the feature is absent. For a plain ERC-20 (non-wNEAR) exit, `transfer_near` is also `None`. [2](#0-1) 

**Step 3 — `callback_args == default()` → bare `PromiseArgs::Create`, no callback.**

The branch at line 470 checks whether `callback_args` equals its `Default` value (both fields `None`). When `error_refund` is disabled and the token is not wNEAR, this condition is always true, so only a bare `ft_transfer` promise is emitted — **no `exit_to_near_precompile_callback` is ever scheduled**. [3](#0-2) 

**Step 4 — Callback refund path is unreachable.**

Even if the callback were somehow invoked, the refund branch at line 231 requires `args.refund` to be `Some(...)`. Without `error_refund`, it is always `None`, so the `else { None }` arm at line 240 is taken — no `refund_on_error` call, no ERC-20 re-mint. [4](#0-3) 

**Step 5 — The codebase's own test confirms the token loss.**

The integration test `test_exit_to_near_refund` explicitly asserts that without `error_refund`, the ERC-20 balance after a failed `ft_transfer` (unregistered recipient) is `FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT` — i.e., the burned tokens are gone with no refund. [5](#0-4) 

---

### Impact Explanation

A user who calls `withdrawToNear` targeting any NEAR account not registered with the NEP-141 token will:
- Lose their ERC-20 tokens (burned unconditionally).
- Receive zero NEP-141 credit (the `ft_transfer` panics on the NEAR side).
- Have no on-chain recourse (no refund callback, no re-mint).

This is **permanent, irrecoverable loss of user funds** — matching the Critical scope: *"Direct theft of any user funds, whether at-rest or in-motion."*

---

### Likelihood Explanation

- Any user can call `withdrawToNear` directly on any `EvmErc20` mirror contract.
- Unregistered NEAR accounts are common (storage deposit not paid).
- No privilege or admin action is required.
- The only precondition is that the deployed engine binary was compiled without `error_refund` — a valid, documented build configuration explicitly tested in the repository.

---

### Recommendation

1. **Always enable `error_refund` in production builds.** The feature exists precisely to close this gap; deploying without it removes the only on-chain safety net.
2. **Alternatively, guard the burn in `EvmErc20::withdrawToNear`.** Check the precompile return value and revert if it signals failure, so the burn is atomic with a successful promise registration.
3. **Document the invariant break explicitly** in the non-`error_refund` build so operators understand the risk before deploying.

---

### Proof of Concept

The existing integration test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` is a ready-made proof of concept: [6](#0-5) 

Run it against a non-`error_refund` build:

```
cargo test -p aurora-engine-tests test_exit_to_near_refund
```

The test asserts `erc20_balance == FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT`, confirming the burned tokens are permanently lost. The NEP-141 balance of `unregistered.near` remains zero, and the NEP-141 balance held by Aurora is unchanged — the tokens vanished from the EVM with no NEAR-side credit.

### Citations

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

**File:** engine-tests/src/tests/erc20_connector.rs (L623-665)
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
```
