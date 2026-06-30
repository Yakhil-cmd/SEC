Based on my thorough code analysis, this is a **valid vulnerability**. Here is the complete finding:

---

### Title
Missing Refund on Failed `ft_transfer` in ERC-20 Exit-to-NEAR Flow Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

### Summary

When the Aurora Engine is compiled **without** the `error_refund` feature (which is not a default feature), a user who calls the `ExitToNear` precompile to bridge ERC-20 tokens back to NEAR will have their ERC-20 tokens permanently burned with no refund if the downstream `ft_transfer` promise fails (e.g., because the receiver account is not registered with the NEP-141 contract). The user ends up with neither their ERC-20 tokens nor the NEP-141 tokens.

### Finding Description

**Step 1 — `error_refund` is not a default feature.**

In `engine/Cargo.toml`, the default features are `["std"]`. The `error_refund` feature is a separate opt-in:

```toml
default = ["std"]
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [1](#0-0) 

Similarly in `engine-precompiles/Cargo.toml`:

```toml
default = ["std"]
error_refund = []
``` [2](#0-1) 

**Step 2 — `refund` is hardcoded to `None` at compile time without the feature.**

In `ExitToNear::run()` in `native.rs`, the `ExitToNearPrecompileCallbackArgs` is constructed with `refund: None` when `error_refund` is absent:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [3](#0-2) 

**Step 3 — For the standard ERC-20 `ft_transfer` path, `transfer_near_args` is also `None`.**

In `exit_erc20_token_to_near`, the legacy `ft_transfer` branch (the `_` arm) returns `transfer_near_args = None`: [4](#0-3) 

This means `callback_args == ExitToNearPrecompileCallbackArgs::default()` (both fields `None`).

**Step 4 — When `callback_args` equals default, NO callback is scheduled at all.**

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // <-- no callback
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [5](#0-4) 

The `exit_to_near_precompile_callback` function is **never scheduled**. If `ft_transfer` fails on the NEP-141 contract, there is no handler to mint back the burned ERC-20 tokens.

**Step 5 — The ERC-20 burn is already committed before the promise executes.**

The ERC-20 contract calls the precompile from within its burn function. The EVM state change (burning the tokens) is committed as part of the `submit` transaction. The `ft_transfer` promise executes asynchronously afterward. A NEAR promise failure does not revert the EVM state.

**Step 6 — The existing test explicitly confirms this behavior.**

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` directly acknowledges the loss:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [6](#0-5) 

The test asserts that the ERC-20 balance is reduced by `FT_EXIT_AMOUNT` with no recovery — confirming the tokens are permanently gone.

### Impact Explanation

After a failed `ft_transfer`:
- **ERC-20 balance**: permanently zero (burned in EVM, no mint-back)
- **NEP-141 balance of Aurora on the NEP-141 contract**: unchanged (transfer failed, tokens remain locked in Aurora's account on the NEP-141 contract)
- **User's NEP-141 balance**: zero (never received)

The user permanently loses access to both representations of their funds. The NEP-141 tokens are stranded in Aurora's connector account with no corresponding ERC-20 to redeem them. This constitutes **permanent freezing of funds** (Critical scope), not merely temporary.

### Likelihood Explanation

This is reachable by any ordinary user. A user only needs to call the `ExitToNear` precompile (via an ERC-20 contract's withdraw/burn function) with a NEAR recipient account that has not registered storage with the NEP-141 contract. NEP-141 storage registration is a prerequisite that many users are unaware of. The trigger condition is common and requires no special privileges.

### Recommendation

1. **Enable `error_refund` by default** in the production contract build, or promote it to a non-optional part of the `contract` feature.
2. Alternatively, **always schedule the callback** regardless of whether `refund` is `None`, so that a failed `ft_transfer` can at minimum be logged and handled.
3. Add a pre-flight check in the precompile to verify the receiver is registered before burning ERC-20 tokens, reverting the EVM call if not.

### Proof of Concept

1. Compile Aurora Engine **without** `error_refund` (the default).
2. Deploy a NEP-141 token; bridge it to an ERC-20 on Aurora.
3. Call the ERC-20's `withdrawToNear` (which calls the `ExitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) with `receiver = "unregistered.near"`.
4. Observe: ERC-20 balance decreases by the exit amount; `ft_transfer` fails (unregistered receiver); no callback fires; ERC-20 balance is NOT restored.
5. The existing test `test_exit_to_near_refund` (with `#[cfg(not(feature = "error_refund"))]`) already proves this outcome deterministically. [7](#0-6)

### Citations

**File:** engine/Cargo.toml (L43-48)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** engine-precompiles/Cargo.toml (L35-39)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
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
