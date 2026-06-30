### Title
Permanent Token Loss in `ExitToNear` Precompile When `ft_transfer` Fails Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `ExitToNear` precompile is invoked for ERC-20 token withdrawals, the ERC-20 tokens are burned from the EVM state before the NEAR-side `ft_transfer` promise is dispatched. In the production build (compiled with `contract` features only), the `error_refund` feature is absent, so no refund callback is attached to the outgoing promise. If the NEAR-side `ft_transfer` fails for any reason (e.g., unregistered recipient), the burned ERC-20 tokens are permanently lost with no recovery path.

---

### Finding Description

**Production build features** are defined in `Makefile.toml`:

```toml
CARGO_FEATURES_BUILD = "contract"
``` [1](#0-0) 

The `error_refund` feature is a separate opt-in feature not included in `"contract"`:

```toml
[features]
default = ["std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [2](#0-1) 

Inside `ExitToNear::run()` in `engine-precompiles/src/native.rs`, the `refund` field of `ExitToNearPrecompileCallbackArgs` is unconditionally set to `None` when `error_refund` is not compiled in:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [3](#0-2) 

For regular ERC-20 exits (not wNEAR unwrap), `transfer_near_args` is also `None` (see `exit_erc20_token_to_near` returning `None` for `transfer_near_args` in the `ft_transfer` branch): [4](#0-3) 

This makes `callback_args == ExitToNearPrecompileCallbackArgs::default()`, triggering the branch that creates the promise **without any callback**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [5](#0-4) 

Even when a callback IS attached (wNEAR unwrap path, where `transfer_near` is `Some`), the failure branch in `exit_to_near_precompile_callback` silently does nothing because `args.refund` is `None`:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← no refund, tokens permanently lost
};
``` [6](#0-5) 

The `refund_on_error` function, which would re-mint burned ERC-20 tokens, is therefore never reachable in production: [7](#0-6) 

The test suite explicitly documents this behavior — without `error_refund`, the exit amount is permanently deducted:

```rust
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [8](#0-7) 

---

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

When a user calls the `ExitToNear` precompile to bridge ERC-20 tokens back to NEAR:
1. The ERC-20 tokens are burned from the EVM state (irreversible within the EVM).
2. A `ft_transfer` promise is dispatched to the NEP-141 contract on NEAR.
3. If `ft_transfer` fails (recipient not registered, NEP-141 contract paused, insufficient storage deposit, etc.), there is no callback to re-mint the burned tokens.
4. The tokens are permanently destroyed — gone from both the EVM and NEAR sides.

---

### Likelihood Explanation

**High.** The failure condition (`ft_transfer` failing) is easily triggered by any user who specifies a NEAR recipient account that has not registered storage with the NEP-141 token contract. This is a common user error. Additionally, a malicious actor can deliberately trigger this for any victim who calls `ExitToNear` with a crafted recipient. The production build provably lacks the `error_refund` feature, making this a live issue on every deployed Aurora Engine instance built with the standard `cargo make build` pipeline.

---

### Recommendation

Enable the `error_refund` feature in the production build by changing `CARGO_FEATURES_BUILD` in `Makefile.toml`:

```toml
CARGO_FEATURES_BUILD = "contract,error_refund"
```

This ensures `refund_call_args` is populated and the `exit_to_near_precompile_callback` is always attached as a callback to the `ft_transfer` promise, allowing burned ERC-20 tokens to be re-minted if the NEAR-side transfer fails.

---

### Proof of Concept

1. Deploy Aurora Engine with the standard production build (`cargo make build`, features = `"contract"`).
2. Bridge a NEP-141 token into Aurora as an ERC-20.
3. From an EVM address, call the ERC-20's `burn` function (which triggers `ExitToNear`) specifying a NEAR recipient account that has **not** registered storage with the NEP-141 contract.
4. Observe: ERC-20 tokens are burned from the EVM. The `ft_transfer` promise fails on NEAR. No callback fires. No re-mint occurs.
5. The user's ERC-20 balance is reduced by the exit amount, and the NEP-141 balance of the recipient is unchanged — the tokens are permanently destroyed.

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` (lines 623–665) already demonstrates this exact scenario, confirming the balance loss when `error_refund` is absent. [9](#0-8)

### Citations

**File:** Makefile.toml (L8-8)
```text
CARGO_FEATURES_BUILD = "contract"
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
