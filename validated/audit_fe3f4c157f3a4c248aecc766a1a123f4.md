### Title
ERC-20 Tokens Burned Without NEP-141 Refund When `error_refund` Feature Is Disabled - (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is not enabled, the `ExitToNear` precompile does not register a callback for the outgoing NEAR cross-contract call. If the NEP-141 `ft_transfer` call subsequently fails (e.g., unregistered recipient account), the ERC-20 tokens that were already burned on Aurora are permanently lost with no refund path, resulting in direct theft/permanent freeze of user funds.

---

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear::run` function constructs `ExitToNearPrecompileCallbackArgs` with a compile-time conditional:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

Immediately after, the code decides whether to attach a callback at all:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

For a standard (non-wNEAR) ERC-20 exit, `transfer_near` is `None` (returned by `exit_erc20_token_to_near`). Without `error_refund`, `refund` is also `None`. Therefore `callback_args` equals the default, and **no callback is registered**. The NEAR promise is fired as a bare `Create` with no error handler. [3](#0-2) 

The ERC-20 burn happens unconditionally and first, inside `EvmErc20.sol`:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);   // ← tokens destroyed here, before any NEAR call
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, ...)
    }
}
``` [4](#0-3) 

The `exit_to_near_precompile_callback` that would re-mint the tokens on failure is only invoked when a callback was registered:

```rust
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
``` [5](#0-4) 

Without the callback, this code path is never reached. The `refund_on_error` function that would re-mint the ERC-20 tokens is never called. [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing / direct loss of user funds.**

A user who calls `withdrawToNear` with a recipient NEAR account that is not registered with the NEP-141 contract will have their ERC-20 tokens burned on Aurora while receiving zero NEP-141 tokens on NEAR. The tokens are unrecoverable: the ERC-20 supply is reduced, Aurora's NEP-141 balance is unchanged, and no NEAR-side transfer occurred. The user's value is destroyed.

---

### Likelihood Explanation

**Moderate.** The `ft_transfer` NEAR call fails whenever the recipient account has not called `storage_deposit` on the NEP-141 contract. This is a routine prerequisite that many users overlook. The existing test suite explicitly documents and exercises this failure mode:

```rust
// The ft_transfer will fail because this account is not registered with the NEP-141
exit_to_near(&ft_owner, "unregistered.near", FT_EXIT_AMOUNT, &erc20, &aurora).await.unwrap();

#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [7](#0-6) 

The comment "there is no refund in the EVM" confirms the fund loss is a known consequence of the disabled feature, not a handled edge case.

---

### Recommendation

The refund logic in `ExitToNearPrecompileCallbackArgs` should not be gated behind a compile-time feature flag. The callback should always be registered for ERC-20 exits so that a failed NEAR-side transfer always triggers re-minting of the burned ERC-20 tokens. Alternatively, the burn should be deferred until the NEAR-side transfer is confirmed successful (though this is architecturally harder given EVM atomicity constraints). At minimum, the `error_refund` feature must be unconditionally enabled in any production build.

---

### Proof of Concept

**Attack path (no special privileges required):**

1. Alice holds 1,000 units of a bridged ERC-20 token on Aurora (backed 1:1 by NEP-141 tokens held by the Aurora contract on NEAR).
2. Alice calls `withdrawToNear("alice-new.near", 1000)` on the `EvmErc20` contract.
3. `_burn(alice, 1000)` executes immediately — Alice's ERC-20 balance is now 0.
4. The `ExitToNear` precompile fires a bare NEAR `ft_transfer` promise to the NEP-141 contract targeting `alice-new.near`.
5. `alice-new.near` has not called `storage_deposit` on the NEP-141 contract; the `ft_transfer` panics and the promise fails.
6. Because `error_refund` is not enabled, no callback was registered. `exit_to_near_precompile_callback` is never invoked. `refund_on_error` is never called.
7. Alice has lost 1,000 ERC-20 tokens and received 0 NEP-141 tokens. The Aurora contract still holds the original NEP-141 balance, but Alice has no claim on it.

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

**File:** engine-tests/src/tests/erc20_connector.rs (L635-665)
```rust
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
