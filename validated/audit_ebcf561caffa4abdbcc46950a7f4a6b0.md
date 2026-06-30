### Title
Unhandled Failed Promise Result in `exit_to_near_precompile_callback` Causes Permanent Fund Loss — (`engine/src/contract_methods/connector.rs`)

---

### Summary

When the `error_refund` compile-time feature is absent (the default production configuration), the `exit_to_near_precompile_callback` function silently ignores a failed `ft_transfer`/`ft_transfer_call` NEAR promise. Because ERC-20 tokens or ETH are burned inside the EVM *before* the asynchronous promise executes, a failed promise leaves the user with no tokens on either side of the bridge — a permanent fund loss with no recovery path.

---

### Finding Description

**Root cause — `engine-precompiles/src/native.rs`**

When the `ExitToNear` precompile constructs its callback arguments, the `refund` field is unconditionally set to `None` unless the `error_refund` feature is compiled in:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // always None in the default production build
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

**Unchecked failure path — `engine/src/contract_methods/connector.rs`**

The callback *does* read the promise result, but when the promise has failed and `args.refund` is `None`, execution falls through to a silent `None` return — no error is raised, no refund is issued, and the function returns `Ok(None)`:

```rust
let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
    // success: optionally transfer NEAR
    None
} else if let Some(args) = args.refund {
    // failure + refund enabled: re-mint / refund
    ...
} else {
    None   // ← silent no-op; tokens already burned in EVM
};
``` [2](#0-1) 

The analogy to the external report is exact: just as Solidity's `(bool success,) = addr.call{value: v}("")` captures but never checks `success`, here the promise result is read but the failure branch is structurally unreachable in the default build, so the failed cross-contract call is silently swallowed.

---

### Impact Explanation

**Critical — Permanent freezing / loss of funds.**

The sequence is:

1. An EVM user calls the `exit_to_near` precompile (ETH path or ERC-20 path).
2. The EVM immediately burns the user's tokens (ETH deducted from balance, or ERC-20 tokens burned via `mint` reversal).
3. A NEAR `ft_transfer` / `ft_transfer_call` promise is scheduled.
4. The promise fails (e.g., recipient account not registered with the NEP-141 contract, insufficient storage deposit, or any other NEAR-side rejection).
5. `exit_to_near_precompile_callback` is invoked; `args.refund` is `None`; the function returns `Ok(None)`.
6. No re-mint, no ETH credit, no NEAR transfer — the user's assets are gone permanently.

The test suite explicitly confirms this outcome:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [3](#0-2) 

---

### Likelihood Explanation

**High.** The trigger condition — a failed `ft_transfer` promise — is reachable by any unprivileged EVM user:

- Sending to a NEAR account that has never registered storage with the NEP-141 contract is sufficient.
- No admin action, no key compromise, and no external oracle is required.
- The `error_refund` feature is absent from the default build, so every production deployment is affected unless the operator explicitly opts in.

---

### Recommendation

1. **Enable `error_refund` unconditionally** — remove the feature gate and always populate `refund` in `ExitToNearPrecompileCallbackArgs`. The refund logic already exists and is tested; it simply needs to be always compiled in.
2. **Alternatively**, make `exit_to_near_precompile_callback` return a hard error (panic / `ContractError`) when the promise fails and no refund args are present, so the failure is at least observable on-chain.
3. **Audit all other callback entry-points** for the same pattern: a checked promise result whose failure branch silently returns `Ok(…)` without restoring state.

---

### Proof of Concept

1. Deploy Aurora without the `error_refund` feature (default).
2. Fund an EVM address with ETH on Aurora.
3. Call the `exit_to_near` precompile targeting a NEAR account that has **not** registered storage with the ETH connector NEP-141 contract.
4. Observe: the EVM balance is debited immediately; the `ft_transfer` promise fails; `exit_to_near_precompile_callback` runs and returns `Ok(None)`; the EVM balance is never restored.

The existing workspace test `test_exit_to_near_eth_refund` in `engine-tests/src/tests/erc20_connector.rs` reproduces this exactly — it drains the Aurora account to force the `ft_transfer` to fail and then asserts the reduced balance when `error_refund` is absent. [4](#0-3) [5](#0-4)

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

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

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

        Ok(maybe_result)
    })
}
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
