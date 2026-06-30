### Title
Missing Refund Path on `ft_transfer` Failure When `error_refund` Feature Is Disabled — (`engine-precompiles/src/native.rs`)

---

### Summary

When the Aurora Engine is compiled without the `error_refund` Cargo feature, the `exit_to_near` precompile schedules the NEAR-side `ft_transfer` (or `ft_transfer_call`) as a bare `PromiseArgs::Create` with no callback. If that promise fails on the NEAR side, the user's EVM balance has already been debited and there is no code path to restore it. The codebase explicitly acknowledges and tests this behavior.

---

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear::run` method constructs `callback_args` as follows:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None without the feature
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

For the two most common exit paths — ETH base-token exit (flag `0x0`, non-omni) and ERC-20 exit (flag `0x1`, non-wNEAR, non-omni) — `transfer_near_args` is also `None`: [2](#0-1) [3](#0-2) 

This means `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()` (`{ refund: None, transfer_near: None }`), triggering the branch:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(...)              // ← callback with refund logic
};
``` [4](#0-3) 

A bare `PromiseArgs::Create` schedules only the `ft_transfer` call with no `then`-callback. If `ft_transfer` fails (e.g., unregistered recipient, paused eth-connector, insufficient NEP-141 balance), NEAR simply discards the failed receipt. The `exit_to_near_precompile_callback` function — which contains the only refund logic — is never invoked: [5](#0-4) 

The `ExitToNearPrecompileCallbackArgs` struct definition confirms `refund` is `Option<RefundCallArgs>`: [6](#0-5) 

---

### Impact Explanation

The user's EVM balance (ETH or ERC-20) is debited at EVM execution time. If the downstream NEAR promise fails, the debited amount is permanently lost from the user's perspective — it is neither credited to the NEAR recipient nor returned to the EVM address. This breaks the invariant that `sum(EVM balances) + NEP-141 supply = constant`.

Impact: **High — Temporary (or permanent) freezing of funds** equal to the exit amount.

---

### Likelihood Explanation

The precondition is an engine deployment compiled without the `error_refund` Cargo feature. The failure trigger requires only a valid EVM transaction calling the `exit_to_near` precompile with a NEAR recipient that is not registered with the NEP-141 contract (or any other condition that causes `ft_transfer` to revert). This is a normal user-reachable path requiring no special privileges.

The codebase's own integration tests explicitly confirm the fund-loss behavior when the feature is absent:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [7](#0-6) 

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [8](#0-7) 

---

### Recommendation

1. **Require `error_refund` for production builds.** Add a `#[cfg(not(feature = "error_refund"))] compile_error!(...)` guard or document it as a mandatory production feature.
2. **Alternatively**, unconditionally attach the callback for the failure branch even without `error_refund`, and have the callback be a no-op on success but log the failure. This preserves the refund path regardless of feature flags.
3. **At minimum**, document clearly in deployment guides that omitting `error_refund` creates a permanent fund-loss risk on `ft_transfer` failure.

---

### Proof of Concept

1. Deploy Aurora Engine compiled **without** `--features error_refund`.
2. Bridge a NEP-141 token to create an ERC-20 mirror on Aurora.
3. Call the `exit_to_near` precompile from an EVM contract with a recipient NEAR account that is **not registered** with the NEP-141 contract (storage deposit not paid).
4. Observe: ERC-20 balance on Aurora is debited; `ft_transfer` fails on NEAR side; no callback fires; no refund is issued.
5. Assert: `erc20_balance(user) == initial - exit_amount` (funds gone), `nep141_balance(recipient) == 0` (transfer never landed).

This matches the behavior explicitly tested in `test_exit_to_near_refund` and `test_exit_to_near_eth_refund` under `#[cfg(not(feature = "error_refund"))]`. [9](#0-8) [10](#0-9)

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

**File:** engine-precompiles/src/native.rs (L536-553)
```rust
        None => Ok((
            eth_connector_account_id,
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            format!(
                r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                exit_params.receiver_account_id,
                context.apparent_value.as_u128()
            ),
            events::ExitToNear::Legacy(ExitToNearLegacy {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
            }),
            "ft_transfer".to_string(),
            None,
        )),
```

**File:** engine-precompiles/src/native.rs (L627-646)
```rust
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
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

**File:** engine-types/src/parameters/connector.rs (L130-134)
```rust
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq, Default)]
pub struct ExitToNearPrecompileCallbackArgs {
    pub refund: Option<RefundCallArgs>,
    pub transfer_near: Option<TransferNearArgs>,
}
```

**File:** engine-tests/src/tests/erc20_connector.rs (L623-666)
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
