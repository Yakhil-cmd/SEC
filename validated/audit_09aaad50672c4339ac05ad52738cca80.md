### Title
ERC-20 Tokens Permanently Burned With No Refund When NEAR-Side `ft_transfer` Fails in `ExitToNear` Precompile (Without `error_refund` Feature) - (`engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile burns ERC-20 tokens on Aurora before dispatching a NEAR-side `ft_transfer` or `ft_transfer_call` promise. In the production build — which compiles with `CARGO_FEATURES_BUILD = "contract"` and does **not** include the `error_refund` feature — there is no callback to re-mint tokens if the NEAR-side promise fails. Any failure of the NEAR transfer (e.g., unregistered recipient, non-existent account) results in the burned ERC-20 tokens being permanently lost with no recovery path.

---

### Finding Description

The `ExitToNear` precompile at address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` handles ERC-20 bridge exits from Aurora to NEAR. When a user calls it with flag `0x01` (ERC-20 path), the ERC-20 tokens are burned on the Aurora side, and a NEAR promise (`ft_transfer` or `ft_transfer_call`) is dispatched to the NEP-141 contract.

The refund mechanism — which would re-mint the burned tokens if the NEAR promise fails — is gated entirely behind the `error_refund` compile-time feature: [1](#0-0) 

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
```

When `refund` is `None`, the promise is created without a callback: [2](#0-1) 

The production build is defined in `Makefile.toml` as: [3](#0-2) 

```
CARGO_FEATURES_BUILD = "contract"
```

The `error_refund` feature is **not** included. It is defined as an opt-in feature in `engine/Cargo.toml`: [4](#0-3) 

```
error_refund = ["aurora-engine-precompiles/error_refund"]
```

And in `engine-precompiles/Cargo.toml`: [5](#0-4) 

```
error_refund = []
```

The `refund_on_error` function in `engine/src/engine.rs` — which re-mints burned ERC-20 tokens — is only reachable through the callback path that is compiled out: [6](#0-5) 

The test suite explicitly confirms that without `error_refund`, tokens are permanently lost on NEAR-side failure: [7](#0-6) 

```rust
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

When a user calls the `ExitToNear` precompile to bridge ERC-20 tokens to NEAR and the NEAR-side `ft_transfer` fails (e.g., the recipient account is not registered with the NEP-141 contract, the account does not exist, or the NEP-141 contract rejects the transfer), the ERC-20 tokens that were burned on Aurora are permanently destroyed. There is no re-mint, no refund, and no recovery path in the production binary. The tokens are gone from both chains. [8](#0-7) 

---

### Likelihood Explanation

**High.** The `ExitToNear` precompile is a core, publicly documented bridge exit path callable by any EVM user or contract. NEAR-side `ft_transfer` failures are realistic and common: a user who mistypes a NEAR account ID, specifies an account not registered with the NEP-141 token, or targets an account that does not exist will trigger this path. No special privileges or attacker coordination are required — a simple user error is sufficient to trigger permanent loss. [9](#0-8) 

---

### Recommendation

Enable the `error_refund` feature in the production build by adding it to `CARGO_FEATURES_BUILD` in `Makefile.toml`:

```
CARGO_FEATURES_BUILD = "contract,error_refund"
```

This ensures that `refund_call_args` is compiled in, a callback is always registered with the NEAR promise, and `refund_on_error` is invoked to re-mint burned ERC-20 tokens (or refund ETH) whenever the NEAR-side transfer fails. [10](#0-9) 

---

### Proof of Concept

1. Alice holds 100 units of a NEP-141-backed ERC-20 token on Aurora.
2. Alice calls the `ExitToNear` precompile (address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) with flag `0x01`, amount `100`, and recipient `unregistered.near` (an account not registered with the NEP-141 contract).
3. The precompile burns Alice's 100 ERC-20 tokens on Aurora.
4. A NEAR `ft_transfer` promise is dispatched to the NEP-141 contract targeting `unregistered.near`.
5. The NEP-141 contract rejects the transfer because `unregistered.near` is not a registered storage account.
6. Because `error_refund` is not compiled in, no callback exists to re-mint the tokens.
7. Alice's 100 ERC-20 tokens are permanently destroyed. She receives nothing on NEAR and has no tokens on Aurora. [1](#0-0) [2](#0-1)

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

**File:** engine-precompiles/src/native.rs (L558-583)
```rust
fn exit_erc20_token_to_near<I: IO>(
    context: &Context,
    exit_params: &Erc20TokenParams,
    io: &I,
) -> Result<
    (
        AccountId,
        String,
        events::ExitToNear,
        String,
        Option<TransferNearArgs>,
    ),
    ExitError,
> {
    // In case of withdrawing ERC-20 tokens, the `apparent_value` should be zero. In opposite way
    // the funds will be locked in the address of the precompile without any possibility
    // to withdraw them in the future. So, in case if the `apparent_value` is not zero, the error
    // will be returned to prevent that.
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }

    let erc20_address = context.caller; // because ERC-20 contract calls the precompile.
    let nep141_account_id = get_nep141_from_erc20(erc20_address.as_bytes(), io)?;
```

**File:** Makefile.toml (L8-9)
```text
CARGO_FEATURES_BUILD = "contract"
CARGO_FEATURES_BUILD_TEST = "contract,integration-test"
```

**File:** engine/Cargo.toml (L48-48)
```text
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** engine-precompiles/Cargo.toml (L39-39)
```text
error_refund = []
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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```
