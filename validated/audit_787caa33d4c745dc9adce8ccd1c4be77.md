### Title
Missing Refund on Failed `ExitToNear` Bridge Promise When `error_refund` Feature Is Disabled - (`engine-precompiles/src/native.rs`)

### Summary

The `ExitToNear` precompile burns ERC-20 tokens (or deducts ETH) on the Aurora EVM side before issuing a NEAR `ft_transfer` / `ft_transfer_call` promise. The refund callback that would re-mint those tokens on failure is compiled out when the `error_refund` Cargo feature is absent. Because `error_refund` is not included in the `default` or `contract` feature sets, a production build has no recovery path: if the outbound NEAR promise fails, the user's tokens are permanently destroyed.

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear::run` method constructs callback arguments for the `exit_to_near_precompile_callback`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None in production
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When `error_refund` is absent, `refund` is `None`. For the two most common exit paths — legacy `ft_transfer` for ERC-20 tokens and legacy `ft_transfer` for ETH — `transfer_near` is also `None`: [2](#0-1) [3](#0-2) 

When both fields are `None`, `callback_args == ExitToNearPrecompileCallbackArgs::default()`, so the code takes the branch that emits a bare `PromiseArgs::Create` with **no callback attached**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // no callback
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [4](#0-3) 

The `error_refund` feature is defined in `engine-precompiles/Cargo.toml` but is **not** listed under `default` or `contract`:

```toml
[features]
default = ["std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
error_refund = []          # never pulled in by default or contract
``` [5](#0-4) [6](#0-5) 

The same absence holds in the top-level `engine` crate. The refund logic in `engine/src/engine.rs` (`refund_on_error`) and the callback handler `exit_to_near_precompile_callback` in `engine/src/contract_methods/connector.rs` are fully implemented and correct, but they are never reached because no callback promise is scheduled: [7](#0-6) [8](#0-7) 

The engine's own test suite explicitly acknowledges the loss:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [9](#0-8) 

### Impact Explanation

When the outbound `ft_transfer` promise fails (recipient not registered with the NEP-141 token, NEP-141 contract paused, gas exhaustion, etc.) the ERC-20 tokens that were burned inside the EVM are never re-minted. The corresponding NEP-141 balance remains in Aurora's custody with no mechanism to return it to the user. This constitutes **permanent loss of user funds** — a Critical impact.

### Likelihood Explanation

The failure condition is realistic and requires no attacker: a user who calls `ExitToNear` targeting a NEAR account that has not registered storage with the NEP-141 contract will trigger a failed `ft_transfer`. This is a common mistake. The NEP-141 storage-registration requirement is a well-known footgun on NEAR, and the Aurora bridge is a primary user-facing entry point.

### Recommendation

Add `error_refund` to the `contract` feature in `engine/Cargo.toml` and `engine-precompiles/Cargo.toml`:

```toml
contract = ["log", "aurora-engine-sdk/contract",
            "aurora-engine-precompiles/contract",
            "aurora-engine-precompiles/error_refund"]   # add this
```

This ensures the `refund_call_args` path is compiled in for production builds and the `exit_to_near_precompile_callback` will re-mint tokens whenever the outbound NEAR promise fails.

### Proof of Concept

1. Deploy Aurora with the `contract` feature (no `error_refund`).
2. Bridge a NEP-141 token to Aurora, obtaining ERC-20 tokens.
3. Call `ExitToNear` targeting a NEAR account that has **not** registered storage with the NEP-141 contract.
4. The ERC-20 tokens are burned; the `ft_transfer` promise fails.
5. Observe: no `exit_to_near_precompile_callback` is ever scheduled; the ERC-20 balance is zero; the NEP-141 balance of the target account is zero; the NEP-141 balance held by Aurora is unchanged. The user's tokens are gone.

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` already demonstrates this exact scenario and explicitly shows the token loss when the feature is absent. [10](#0-9)

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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
```

**File:** engine/Cargo.toml (L42-50)
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

**File:** engine/src/engine.rs (L1176-1204)
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
