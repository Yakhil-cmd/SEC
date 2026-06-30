### Title
Permanent ERC-20 Token Loss When `exit_to_near` `ft_transfer` Promise Fails Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is absent (it is **not** in the `default` feature set), ERC-20 tokens burned from a user's Aurora EVM balance during an `exit_to_near` call are permanently destroyed if the downstream `ft_transfer` NEP-141 promise fails. No refund path exists in the callback. Any unprivileged EVM user who calls `exit_to_near` with a recipient that is not registered with the NEP-141 contract (or any other condition that causes `ft_transfer` to fail) will suffer an irrecoverable loss of their bridged tokens.

---

### Finding Description

The `exit_to_near` precompile burns ERC-20 tokens from the caller's EVM balance and schedules a NEAR cross-contract `ft_transfer` (or `ft_transfer_call`) promise. A callback (`exit_to_near_precompile_callback`) is attached to handle the result.

The callback args struct is constructed with the `refund` field gated behind the `error_refund` feature flag:

```rust
// engine-precompiles/src/native.rs
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None when feature is absent
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

In the callback, when the `ft_transfer` promise fails and `args.refund` is `None`, the `else` branch is taken and **nothing happens** — no re-mint, no ETH transfer back:

```rust
// engine/src/contract_methods/connector.rs
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← silent no-op; tokens are gone
};
``` [2](#0-1) 

The `error_refund` feature is **not** listed in the `default` feature set of `engine/Cargo.toml`:

```toml
[features]
default = ["std"]
...
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [3](#0-2) 

This means a production binary compiled with only the `default` (or `contract`) features will always set `refund: None`, making every failed `ft_transfer` a permanent, silent token burn with no recovery path.

The refund logic that *would* re-mint the burned ERC-20 tokens is fully implemented in `engine::refund_on_error` but is simply never reached: [4](#0-3) 

The existing test suite explicitly documents this loss:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

---

### Impact Explanation

**Critical — Permanent freezing / direct theft of user funds.**

When `ft_transfer` fails (e.g., the NEAR recipient account is not registered with the NEP-141 token contract, the NEP-141 contract is paused, or any other revert condition), the ERC-20 tokens have already been burned from the user's Aurora EVM balance. Without `error_refund`, the callback silently exits with `None`, leaving the user with neither their EVM tokens nor the corresponding NEP-141 balance. The loss is permanent and unrecoverable through any on-chain mechanism.

---

### Likelihood Explanation

**High.** The trigger condition — a failed `ft_transfer` — is easily reached by any unprivileged EVM user:

- Sending to a NEAR account that has never called `storage_deposit` on the NEP-141 contract (the most common real-world failure mode).
- Sending to a non-existent NEAR account.
- Any NEP-141 contract-side revert.

The attacker-controlled entry path is the standard `exit_to_near` precompile call, which is a documented, public interface available to every Aurora EVM user. No special privileges are required.

---

### Recommendation

Add `error_refund` to the `default` feature set in `engine/Cargo.toml` (and propagate it through `engine-precompiles/Cargo.toml`), or unconditionally populate the `refund` field in `ExitToNearPrecompileCallbackArgs` without gating it behind a compile-time flag. The refund logic in `engine::refund_on_error` is already correct and complete; it simply needs to be reachable on every failed exit path.

---

### Proof of Concept

The existing workspace test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` already demonstrates the loss: [6](#0-5) 

1. Deploy Aurora; bridge a NEP-141 token to an ERC-20 on Aurora.
2. Call `exit_to_near` targeting `"unregistered.near"` (not registered with the NEP-141 contract).
3. The `ft_transfer` promise fails.
4. Without `error_refund`, the callback executes the `else { None }` branch.
5. The user's ERC-20 balance on Aurora is reduced by `FT_EXIT_AMOUNT`; the NEP-141 balance of Aurora's account is unchanged; the user receives nothing. Tokens are permanently lost.

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

**File:** engine/Cargo.toml (L43-48)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
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
