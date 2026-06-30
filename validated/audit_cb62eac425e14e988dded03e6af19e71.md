### Title
ERC-20 Tokens Permanently Burned When `ft_transfer` Fails on NEAR Side Due to Disabled `error_refund` Feature - (`engine-precompiles/src/native.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

When a user calls `withdrawToNear` on `EvmErc20` or `EvmErc20V2`, their ERC-20 tokens are burned first, then the `ExitToNear` precompile schedules a NEAR `ft_transfer` promise. If that NEAR-side promise fails (e.g., recipient not registered for storage), the callback `exit_to_near_precompile_callback` is supposed to re-mint the burned tokens. However, because the `error_refund` compile-time feature is **not** in the default feature set of either `aurora-engine` or `aurora-engine-precompiles`, the refund field is hardcoded to `None` in production builds. The callback silently does nothing on failure, and the burned tokens are permanently lost.

---

### Finding Description

**Step 1 — Burn before precompile call, return value ignored.**

`EvmErc20.sol::withdrawToNear` and `EvmErc20V2.sol::withdrawToNear` both burn the caller's tokens unconditionally before invoking the `ExitToNear` precompile via a low-level assembly `call`. The return value `res` is captured but never checked:

```solidity
// EvmErc20.sol lines 53-63
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // tokens burned here
    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked
    }
}
``` [1](#0-0) 

`EvmErc20V2.sol` has the identical pattern: [2](#0-1) 

**Step 2 — `error_refund` feature is not a default feature.**

`engine/Cargo.toml` declares `default = ["std"]`. The `error_refund` feature is listed separately and is never included in defaults: [3](#0-2) 

`engine-precompiles/Cargo.toml` likewise has `default = ["std"]` with `error_refund` as a standalone opt-in: [4](#0-3) 

**Step 3 — Precompile hardcodes `refund: None` when feature is disabled.**

In `engine-precompiles/src/native.rs`, the `ExitToNear::run` method builds the callback args. Without the `error_refund` feature, `refund` is always `None`: [5](#0-4) 

**Step 4 — Callback does nothing on NEAR-side failure when `refund` is `None`.**

In `engine/src/contract_methods/connector.rs`, `exit_to_near_precompile_callback` checks the NEAR promise result. If the promise failed and `args.refund` is `None`, the `else if` branch is not taken and the function returns `Ok(None)` — silently discarding the failure with no token recovery: [6](#0-5) 

**Step 5 — The codebase itself documents the fund loss.**

The integration test `test_exit_to_near_refund` explicitly acknowledges that without `error_refund`, tokens are permanently lost: [7](#0-6) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any ERC-20 tokens bridged from NEAR (NEP-141) to Aurora (ERC-20) and then withdrawn back via `withdrawToNear` are permanently destroyed if the downstream NEAR `ft_transfer` or `ft_transfer_call` fails. The tokens are burned on the EVM side and never re-minted. There is no recovery path for the user.

---

### Likelihood Explanation

**High.** The NEAR NEP-141 standard requires recipients to pre-register storage before they can receive tokens. A user who calls `withdrawToNear` targeting a NEAR account that has not registered storage with the NEP-141 contract will trigger an `ft_transfer` failure. This is a routine operational mistake. Additionally, any transient NEAR-side error (e.g., insufficient attached gas propagated to the sub-promise, contract paused on the NEAR side) produces the same outcome. The `error_refund` feature is not enabled by default, so every production deployment is affected.

---

### Recommendation

1. **Enable `error_refund` by default** in `engine/Cargo.toml` and `engine-precompiles/Cargo.toml` by adding it to the `default` feature list, or unconditionally include the refund logic without a feature gate.
2. **Alternatively**, in `EvmErc20.sol` and `EvmErc20V2.sol`, check the return value of the assembly `call` and revert if it returns `0`, so the `_burn` is rolled back atomically:
   ```solidity
   assembly {
       let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
       if iszero(res) { revert(0, 0) }
   }
   ```
3. **Structurally**, the burn should occur only after confirmation that the NEAR-side transfer succeeded, or the refund path must be unconditionally active.

---

### Proof of Concept

The existing test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` is the proof of concept. It demonstrates that when `ft_transfer` fails (unregistered recipient), the ERC-20 balance is `FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT` (i.e., the exit amount is gone) when `error_refund` is not enabled, versus `FT_TRANSFER_AMOUNT` (fully refunded) when it is enabled. [8](#0-7) 

To reproduce without the feature:
1. Deploy a NEP-141 token and bridge it to Aurora as an ERC-20.
2. Call `withdrawToNear("unregistered.near", amount)` on the ERC-20 contract.
3. Observe that the ERC-20 balance decreases by `amount` (tokens burned).
4. Observe that `unregistered.near` receives nothing on NEAR (transfer failed).
5. Observe that no re-mint occurs on Aurora — tokens are gone.

### Citations

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
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

**File:** engine/src/contract_methods/connector.rs (L214-242)
```rust
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
