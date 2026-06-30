### Title
Permanent Fund Freeze via `ExitToNear` Precompile Sending to Non-Existent NEAR Recipient Without Refund - (`engine-precompiles/src/native.rs`)

### Summary
The `ExitToNear` precompile burns ETH or ERC-20 tokens from the Aurora EVM side and schedules a NEAR `ft_transfer` promise to a caller-supplied recipient account ID. The account ID is validated only for syntactic format, not for on-chain existence. When the `error_refund` feature is absent from the build (it is **not** a default feature), a failed NEAR promise produces no callback and no refund, permanently destroying the user's tokens.

### Finding Description
`ExitToNear::run()` in `engine-precompiles/src/native.rs` accepts a raw byte slice from the EVM caller and parses it with `parse_recipient()`.

`parse_recipient` calls `AccountId::validate` (via `.parse()`), which checks only that the string is 2–64 bytes long and contains only `[a-z0-9._-]` characters with no consecutive separators. [1](#0-0) 

There is no check that the account actually exists on NEAR or is registered with the NEP-141 token contract. After parsing, the precompile:

1. Burns the ERC-20 tokens (or deducts ETH from the EVM balance).
2. Builds `callback_args`: [2](#0-1) 

   When compiled **without** `error_refund`, `refund` is hardcoded to `None`.

3. Because `callback_args == ExitToNearPrecompileCallbackArgs::default()` (both fields `None`), the promise is scheduled as a bare `PromiseArgs::Create` with **no callback**: [3](#0-2) 

   If the NEAR `ft_transfer` promise fails (account does not exist, not registered, etc.), there is no callback to detect the failure and no refund path is executed.

The `error_refund` feature is **not** in the default feature set of either crate: [4](#0-3) [5](#0-4) 

The `contract` feature (used for production WASM builds) does not pull in `error_refund`: [6](#0-5) 

The `exit_to_near_precompile_callback` handler only issues a refund when `args.refund` is `Some`, which is only populated under `error_refund`: [7](#0-6) 

### Impact Explanation
**Critical – Permanent freezing of funds.** When a user calls the `ExitToNear` precompile with a syntactically valid but non-existent (or unregistered) NEAR account ID, their ETH or ERC-20 tokens are irreversibly burned on the EVM side. The NEAR `ft_transfer` promise fails silently, and without the `error_refund` callback, the tokens are gone forever. There is no recovery path.

### Likelihood Explanation
The `ExitToNear` precompile is a core, publicly reachable bridge exit path callable by any EVM user. NEAR account IDs are human-readable strings; a single-character typo (e.g., `"alice.neer"` instead of `"alice.near"`) passes `AccountId::validate` and triggers the loss. The codebase's own test suite explicitly documents this outcome: [8](#0-7) [9](#0-8) 

### Recommendation
1. **Enable `error_refund` by default** in the production `contract` feature so that a failed NEAR promise always triggers `exit_to_near_precompile_callback` with a populated `refund` field, restoring tokens to the caller's EVM address.
2. Alternatively, add the `error_refund` flag to the `contract` feature dependency chain in `engine/Cargo.toml` and `engine-precompiles/Cargo.toml`.
3. As a defense-in-depth measure, document clearly that callers bear responsibility for supplying a registered recipient, and surface a warning in the precompile output when the feature is absent.

### Proof of Concept
1. Compile Aurora Engine with `--features contract` (no `error_refund`).
2. Fund an EVM address with ETH on Aurora.
3. Call the `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) with input `\x00` + `"typo.neer"` (syntactically valid, non-existent NEAR account) and attach ETH value.
4. `parse_recipient` succeeds; ETH is deducted from the EVM balance; a bare `ft_transfer` promise is scheduled.
5. The NEAR runtime rejects `ft_transfer` because `"typo.neer"` does not exist.
6. No callback fires; no refund is issued.
7. The ETH is permanently destroyed — confirmed by the `#[cfg(not(feature = "error_refund"))]` branch in `test_exit_to_near_eth_refund`. [10](#0-9) [11](#0-10)

### Citations

**File:** engine-precompiles/src/native.rs (L359-379)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
        || (recipient, None),
        |(recipient, msg)| {
            if msg == UNWRAP_WNEAR_MSG {
                (recipient, Some(Message::UnwrapWnear))
            } else {
                (recipient, Some(Message::Omni(msg)))
            }
        },
    );

    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
}
```

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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
```

**File:** engine/Cargo.toml (L42-51)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
integration-test = ["log"]
all-promise-actions = ["aurora-engine-sdk/all-promise-actions"]
impl-serde = ["aurora-engine-types/impl-serde", "aurora-engine-transactions/impl-serde", "aurora-evm/with-serde"]
```

**File:** engine/src/contract_methods/connector.rs (L196-245)
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

**File:** engine-tests/src/tests/erc20_connector.rs (L771-780)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);

        assert_eq!(
            eth_balance_of(signer_address, &aurora).await,
            expected_balance
        );
```
