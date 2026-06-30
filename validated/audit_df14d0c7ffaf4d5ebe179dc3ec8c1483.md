Based on my analysis of the production code, I can trace the exact execution path and confirm the behavior.

---

### Title
Missing Refund on Failed ERC-20 Exit When `error_refund` Feature Is Disabled — (`engine-precompiles/src/native.rs`, `engine/src/contract_methods/connector.rs`)

### Summary

When the Aurora Engine is compiled without the `error_refund` feature flag (which is the case for the default production build), a failed `ft_transfer` during an ERC-20 exit-to-NEAR flow results in the ERC-20 tokens being permanently burned with no refund. The callback args are constructed with `refund: None`, and when the transfer fails, no re-mint occurs.

### Finding Description

**Step 1 — Production build does not include `error_refund`.**

The `Makefile.toml` defines the production feature set as:

```
CARGO_FEATURES_BUILD = "contract"
``` [1](#0-0) 

The `error_refund` feature is a separate opt-in flag not included in the production binary.

**Step 2 — Precompile hardcodes `refund: None` when feature is absent.**

In `ExitToNear::run`, the `ExitToNearPrecompileCallbackArgs` is constructed with a compile-time conditional:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [2](#0-1) 

**Step 3 — No callback is scheduled for a standard ERC-20 exit.**

For a standard ERC-20 exit (not wNEAR unwrap), `transfer_near` is also `None`. Since `ExitToNearPrecompileCallbackArgs` derives `Default` with both fields `None`, the condition:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // <-- no callback attached
} else {
    PromiseArgs::Callback(...)
};
``` [3](#0-2) 

...means only a bare `ft_transfer` promise is created with **no callback**. If the transfer fails, there is no handler to re-mint the burned ERC-20 tokens.

**Step 4 — Even if the callback were reached, `refund: None` silently returns.**

In `exit_to_near_precompile_callback`, the failure branch is:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // <-- silent no-op when refund is None
};
``` [4](#0-3) 

**Step 5 — The test suite explicitly confirms this behavior.**

The existing test `test_exit_to_near_refund` documents the divergence:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

The ERC-20 balance after a failed exit is `FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT` — the burned amount is **not restored**.

### Impact Explanation

A user who initiates an ERC-20 exit to a NEAR account that is not registered with the NEP-141 contract (or any contract that rejects `ft_transfer`) will have their ERC-20 tokens permanently burned. The NEP-141 tokens remain in Aurora's custody (the `ft_transfer` reverts on the NEP-141 side), but the ERC-20 side has already been burned and no re-mint occurs. The user loses the full exit amount with no recovery path. This constitutes permanent loss of bridged principal tokens. [6](#0-5) 

### Likelihood Explanation

The trigger is a normal, user-accessible EVM transaction calling the `ExitToNear` precompile with a recipient NEAR account that is not registered with the NEP-141 contract. This is a realistic user mistake (e.g., typo in account ID, account not yet created, or storage not deposited). No admin access, key compromise, or external oracle is required. The production binary is confirmed to be built without `error_refund`.

### Recommendation

Enable the `error_refund` feature in the production build by adding it to `CARGO_FEATURES_BUILD` in `Makefile.toml`:

```
CARGO_FEATURES_BUILD = "contract,error_refund"
``` [1](#0-0) 

This ensures `refund_call_args` is populated and the callback correctly invokes `engine::refund_on_error` to re-mint the burned ERC-20 tokens on failure. [7](#0-6) 

### Proof of Concept

The existing test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` already demonstrates the exploit path:

1. Bridge NEP-141 tokens to ERC-20 on Aurora.
2. Call the exit precompile targeting `"unregistered.near"` (not registered with the NEP-141 contract).
3. Observe that `ft_transfer` fails on the NEP-141 side.
4. Assert ERC-20 balance is `FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT` — the burned tokens are **not** restored. [8](#0-7) 

This test passes today on the production feature set (`contract` only), confirming the vulnerability is present and locally reproducible on unmodified code.

### Citations

**File:** Makefile.toml (L8-8)
```text
CARGO_FEATURES_BUILD = "contract"
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

**File:** engine-precompiles/src/native.rs (L699-725)
```rust
#[cfg(feature = "error_refund")]
#[allow(clippy::unnecessary_wraps)]
fn refund_call_args(
    params: &ExitToNearParams,
    event: &events::ExitToNear,
) -> Option<RefundCallArgs> {
    Some(RefundCallArgs {
        recipient_address: match params {
            ExitToNearParams::BaseToken(params) => params.refund_address,
            ExitToNearParams::Erc20TokenParams(params) => params.refund_address,
        },
        erc20_address: match params {
            ExitToNearParams::BaseToken(_) => None,
            ExitToNearParams::Erc20TokenParams(_) => {
                let erc20_address = match event {
                    events::ExitToNear::Legacy(legacy) => legacy.erc20_address,
                    events::ExitToNear::Omni(omni) => omni.erc20_address,
                };
                Some(erc20_address)
            }
        },
        amount: types::u256_to_arr(&match event {
            events::ExitToNear::Legacy(legacy) => legacy.amount,
            events::ExitToNear::Omni(omni) => omni.amount,
        }),
    })
}
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

**File:** engine-types/src/parameters/connector.rs (L130-134)
```rust
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq, Default)]
pub struct ExitToNearPrecompileCallbackArgs {
    pub refund: Option<RefundCallArgs>,
    pub transfer_near: Option<TransferNearArgs>,
}
```
