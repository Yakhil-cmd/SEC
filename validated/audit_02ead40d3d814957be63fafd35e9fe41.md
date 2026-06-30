### Title
`exit_to_near` Precompile Wrapper Does Not Account for Refunds on Failed NEAR `ft_transfer` — (`engine-precompiles/src/native.rs`, `engine/src/contract_methods/connector.rs`)

---

### Summary

The `exit_to_near` precompile in Aurora Engine burns ERC-20 tokens (or ETH) from the user's EVM balance and then issues a NEAR `ft_transfer` / `ft_transfer_call` promise. When that NEAR call fails, the callback is responsible for re-minting the burned tokens. However, the refund information is only populated when the `error_refund` compile-time feature is enabled. Without it, the callback receives `refund: None` and silently does nothing, permanently destroying the user's funds.

---

### Finding Description

**Step 1 — Tokens are burned before the NEAR call.**

The `exit_to_near` precompile burns the caller's ERC-20 tokens (or deducts ETH) as part of the exit flow, then schedules a NEAR `ft_transfer` or `ft_transfer_call` promise.

**Step 2 — Callback args are built with `refund: None` when feature is absent.**

In `engine-precompiles/src/native.rs` around lines 449–455, the `ExitToNearPrecompileCallbackArgs` struct is constructed:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← refund info is discarded
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

**Step 3 — Callback does nothing on failure when `refund` is `None`.**

In `engine/src/contract_methods/connector.rs`, the `exit_to_near_precompile_callback` function handles the promise result:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← no refund, no error, silent loss
};
``` [2](#0-1) 

When `args.refund` is `None` (the non-`error_refund` build), the `else` branch is taken: no re-mint, no ETH transfer back, no error. The burned tokens are gone.

**Step 4 — The test suite explicitly confirms the fund loss.**

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [3](#0-2) 

The same pattern is confirmed for ETH exits:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [4](#0-3) 

---

### Impact Explanation

**Critical — Permanent loss of user funds.**

Any user who calls the `exit_to_near` precompile on a build where `error_refund` is not enabled will have their ERC-20 tokens (or ETH) permanently destroyed if the downstream NEAR `ft_transfer` call fails. The NEAR call can fail for entirely ordinary reasons: the recipient account is not registered with the NEP-141 token contract, insufficient storage deposit, or the token contract is paused. The user has no way to recover the burned assets. [5](#0-4) 

---

### Likelihood Explanation

**Medium.**

The `ft_transfer` NEAR call fails whenever the recipient account is not registered with the NEP-141 contract (a common user error), or when the Aurora contract's storage deposit is exhausted. Both conditions are reachable by any unprivileged user without any special setup. The vulnerability is only absent when the `error_refund` feature is compiled in; any deployment that omits this feature is permanently vulnerable. [1](#0-0) 

---

### Recommendation

1. **Make `error_refund` a default feature** in `engine-precompiles/Cargo.toml` and `engine/Cargo.toml` so that refund logic is always compiled in.
2. Alternatively, remove the `#[cfg(feature = "error_refund")]` guards entirely and unconditionally populate `refund` in `ExitToNearPrecompileCallbackArgs`, making the refund path always active.
3. Add a compile-time or runtime assertion that prevents deployment without the refund path enabled.

---

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature flag.
2. Bridge a NEP-141 token to Aurora (user holds ERC-20 balance).
3. Call the `exit_to_near` precompile specifying an **unregistered** NEAR account as the recipient.
4. The precompile burns the user's ERC-20 tokens and schedules `ft_transfer` to the unregistered account.
5. The NEAR `ft_transfer` call fails (unregistered account).
6. `exit_to_near_precompile_callback` is invoked; `args.refund` is `None`; the `else` branch executes and returns `None` with no re-mint.
7. Observe: the user's ERC-20 balance is permanently zero; no refund was issued. Confirmed by the test assertion at `engine-tests/src/tests/erc20_connector.rs:658–660`. [6](#0-5) [7](#0-6)

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

**File:** engine-tests/src/tests/erc20_connector.rs (L717-781)
```rust
    #[tokio::test]
    async fn test_exit_to_near_eth_refund() {
        // Test the case where the ft_transfer promise from the exit call fails;
        // ensure ETH is refunded.

        let TestExitToNearEthContext {
            signer,
            signer_address,
            chain_id,
            tester_address,
            aurora,
        } = test_exit_to_near_eth_common().await.unwrap();
        let exit_account_id = "any.near";

        // Make the ft_transfer call fail by draining the Aurora account
        let result = aurora
            .ft_transfer(
                &"tmp.near".parse().unwrap(),
                u128::from(INITIAL_ETH_BALANCE).into(),
                &None,
            )
            .max_gas()
            .deposit(NearToken::from_yoctonear(1))
            .transact()
            .await
            .unwrap();
        assert!(result.is_success());

        // call exit to near
        let input = build_input(
            "withdrawEthToNear(bytes)",
            &[ethabi::Token::Bytes(exit_account_id.as_bytes().to_vec())],
        );
        let tx = utils::create_eth_transaction(
            Some(tester_address),
            Wei::new_u64(ETH_EXIT_AMOUNT),
            input,
            Some(chain_id),
            &signer.secret_key,
        );
        let result = aurora
            .submit(rlp::encode(&tx).to_vec())
            .max_gas()
            .transact()
            .await
            .unwrap();
        assert!(result.is_success());

        // check balances
        assert_eq!(
            nep_141_balance_of(aurora.as_raw_contract(), &exit_account_id.parse().unwrap()).await,
            0
        );

        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);

        assert_eq!(
            eth_balance_of(signer_address, &aurora).await,
            expected_balance
        );
    }
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
