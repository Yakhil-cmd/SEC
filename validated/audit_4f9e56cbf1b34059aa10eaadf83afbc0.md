### Title
No Transfer Amount Limits in `ExitToNear` Precompile Combined with Disabled-by-Default Refund Feature Causes Permanent Fund Loss — (`engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile enforces no upper bound on bridge transfer amounts. When the downstream NEAR-side promise fails (e.g., unregistered recipient, insufficient NEP-141 liquidity, or receiving contract rejection), the ERC-20 tokens are already burned on the Aurora EVM side. Because the `error_refund` feature is **not** a default feature, no callback is attached to re-mint the burned tokens, resulting in permanent loss of user funds.

---

### Finding Description

**Step 1 — No amount limit.**

`parse_amount` in `engine-precompiles/src/native.rs` only enforces a type-safety ceiling (`> u128::MAX`), not any protocol-level upper bound: [1](#0-0) 

Neither `ExitToNear::run` nor `ExitToEthereum::run` adds any further cap before constructing the NEAR promise.

**Step 2 — Tokens are burned before the promise is dispatched.**

In `EvmErc20.sol`, `_burn` is called unconditionally before the precompile call: [2](#0-1) 

**Step 3 — Refund callback is gated behind a non-default compile feature.**

`engine-precompiles/Cargo.toml` lists `error_refund` as an opt-in feature, absent from `default`: [3](#0-2) 

`engine/Cargo.toml` mirrors this — `error_refund` is not in `default`: [4](#0-3) 

**Step 4 — Without `error_refund`, no callback is attached.**

When the feature is absent, `refund` is hardcoded to `None`: [5](#0-4) 

When both `refund` and `transfer_near` are `None`, `callback_args` equals `Default::default()`, so the promise is created without any callback: [6](#0-5) 

If the `ft_transfer` or `ft_transfer_call` NEAR promise fails, there is no mechanism to re-mint the already-burned ERC-20 tokens.

---

### Impact Explanation

**Permanent freezing of funds (Critical).** ERC-20 tokens are irreversibly burned on the Aurora EVM side. If the corresponding NEAR-side promise fails for any reason — unregistered recipient, insufficient NEP-141 liquidity in the receiving contract, or rejection by an Omni-bridge receiver — the tokens are gone. There is no on-chain recovery path when `error_refund` is disabled.

---

### Likelihood Explanation

**Medium.** The failure condition is reachable by any unprivileged EVM user:

- Sending to a NEAR account not registered with the NEP-141 contract is a common user mistake.
- The `ft_transfer_call` (Omni) path can fail if the receiving NEAR contract rejects the transfer or lacks storage.
- The existing test `test_exit_to_near_refund` explicitly documents the permanent-loss outcome when `error_refund` is off: [7](#0-6) 

The same pattern is confirmed for ETH exits in `test_exit_to_near_eth_refund`: [8](#0-7) 

---

### Recommendation

1. **Enable `error_refund` by default** in both `engine-precompiles` and `engine` crates so that a re-mint callback is always attached to the NEAR promise.
2. **Add an upper-bound limit** on bridge amounts in `ExitToNear` and `ExitToEthereum` to reduce the blast radius of any failed promise.
3. Alternatively, restructure the flow so that ERC-20 tokens are only burned *after* the NEAR-side promise succeeds (i.e., move the burn into the callback).

---

### Proof of Concept

1. User holds 1 000 ERC-20 tokens on Aurora (backed 1:1 by NEP-141 tokens held by the Aurora contract).
2. User calls `withdrawToNear("unregistered.near", 1000)` on `EvmErc20`.
3. `_burn(msg.sender, 1000)` executes — tokens are gone from the EVM state.
4. The `ExitToNear` precompile constructs a `ft_transfer` promise targeting `"unregistered.near"`.
5. The NEAR runtime executes the promise; it fails because `"unregistered.near"` has no storage deposit with the NEP-141 contract.
6. Because `error_refund` is not enabled, `callback_args == Default::default()`, so `PromiseArgs::Create` (no callback) was used — no re-mint occurs.
7. User permanently loses 1 000 tokens. The NEP-141 tokens remain locked in the Aurora contract with no corresponding ERC-20 representation.

### Citations

**File:** engine-precompiles/src/native.rs (L337-345)
```rust
fn parse_amount(input: &[u8]) -> Result<U256, ExitError> {
    let amount = U256::from_big_endian(input);

    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }

    Ok(amount)
}
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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
```

**File:** engine/Cargo.toml (L42-49)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
integration-test = ["log"]
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

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
