### Title
Permanent Loss of User Funds When `ft_transfer` Fails During `exit_to_near` Without `error_refund` Feature - (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is not enabled, the `ExitToNear` precompile unconditionally sets `callback_args.refund = None` and, for non-wNEAR exits, emits the `ft_transfer` promise with **no failure callback**. If the downstream `ft_transfer` call fails, the ETH or ERC-20 tokens that were already burned/transferred from the user's EVM balance are permanently unrecoverable.

---

### Finding Description

The `ExitToNear` precompile's `run` function in `engine-precompiles/src/native.rs` constructs `ExitToNearPrecompileCallbackArgs` as follows:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None without the feature
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

For a standard ETH or ERC-20 exit (not a wNEAR unwrap), `transfer_near` is also `None`. This makes `callback_args` equal to the default value, so the promise is emitted **without any callback**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

The sequence of events when `ft_transfer` fails:

1. **ETH exit**: ETH is transferred from the user's EVM balance to `exit_to_near::ADDRESS` (the precompile address) before the promise is dispatched.
2. **ERC-20 exit**: ERC-20 tokens are burned from the user's EVM balance by the ERC-20 contract before calling the precompile.
3. The `ft_transfer` promise is dispatched to the NEP-141 contract.
4. If `ft_transfer` fails (e.g., recipient account not registered, NEP-141 contract paused, insufficient connector balance), there is **no callback** to detect the failure.
5. The `exit_to_near_precompile_callback` is never invoked, so `refund_on_error` is never called. [3](#0-2) 

The refund path in `refund_on_error` — which re-mints burned ERC-20 tokens or transfers ETH back from the precompile address — is entirely unreachable: [4](#0-3) 

The test suite explicitly acknowledges this behavior:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

The same acknowledgment appears for the ETH exit case: [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

- For ETH exits: the ETH is moved to `exit_to_near::ADDRESS` inside the EVM state and is permanently stranded there with no recovery path.
- For ERC-20 exits: the ERC-20 tokens are burned (supply reduced) and the corresponding NEP-141 tokens remain locked in the Aurora contract with no mechanism to re-mint or return them.

In both cases the user suffers a complete, irreversible loss of the exited amount.

---

### Likelihood Explanation

**High.** Any unprivileged EVM user can trigger this by calling `exit_to_near` with a NEAR recipient account that is not registered with the NEP-141 contract (a common condition for new accounts). The `ft_transfer` call will fail with a standard NEP-141 "account not registered" error, and the tokens will be permanently lost. No special privileges or coordination are required.

---

### Recommendation

Enable the `error_refund` feature in the production build, or unconditionally populate `callback_args.refund` with the refund arguments regardless of the feature flag, so that a failure callback is always attached to the `ft_transfer` promise. The callback must invoke `refund_on_error` to re-mint burned ERC-20 tokens or transfer ETH back from `exit_to_near::ADDRESS` to the original sender.

---

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature.
2. Fund an EVM address with ETH or bridge a NEP-141 token to an ERC-20 on Aurora.
3. Call `exit_to_near` targeting a NEAR account that is **not registered** with the NEP-141 contract (e.g., a freshly created account that has never interacted with the token).
4. The EVM-side balance is debited / ERC-20 tokens are burned immediately within the EVM execution.
5. The `ft_transfer` promise fails with `"The account <recipient> is not registered"`.
6. No callback fires; `exit_to_near_precompile_callback` is never called.
7. Observe: the user's EVM balance is permanently reduced, the NEP-141 recipient balance is zero, and the tokens are unrecoverable.

This matches the test `test_exit_to_near_refund` and `test_exit_to_near_eth_refund` which explicitly document the `FT_EXIT_AMOUNT` loss when `error_refund` is absent. [7](#0-6) [8](#0-7)

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
