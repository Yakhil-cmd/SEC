### Title
Permanent Fund Freeze via Missing Error-Refund Callback in `ExitToEthereum` Precompile — (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToEthereum` precompile schedules a NEAR-side `withdraw` promise with no error-recovery callback. If that promise fails for any reason, the ERC-20 tokens already burned in the EVM are permanently destroyed and the corresponding ETH is never released on Ethereum. Unlike `ExitToNear`, which has an `exit_to_near_precompile_callback` refund path, `ExitToEthereum` has no analogous mechanism, creating a structural accounting gap in the bridge.

### Finding Description

**Step 1 — ERC-20 tokens are burned before the NEAR promise is confirmed.**

In `EvmErc20.sol` and `EvmErc20V2.sol`, `withdrawToEthereum` burns the caller's tokens first, then calls the `ExitToEthereum` precompile via a low-level assembly `call`. The return value `res` is never checked:

```solidity
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, ...)
        // res is never inspected — no revert on failure
    }
}
``` [1](#0-0) [2](#0-1) 

**Step 2 — The precompile schedules a fire-and-forget NEAR promise.**

`ExitToEthereum::run()` constructs a `PromiseArgs::Create` (no callback) targeting the eth-connector's `withdraw` method:

```rust
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    ...
    attached_gas: costs::WITHDRAWAL_GAS,
};
let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
``` [3](#0-2) 

There is no `PromiseWithCallbackArgs` wrapping this call, so there is no way for the engine to detect failure and re-mint the burned tokens.

**Step 3 — Contrast with `ExitToNear`, which has a refund callback.**

`ExitToNear::run()` wraps its promise in a `PromiseWithCallbackArgs` that calls `exit_to_near_precompile_callback` on failure. That callback invokes `engine::refund_on_error`, which re-mints burned ERC-20 tokens or returns ETH from the precompile address:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs {
        base: transfer_promise,
        callback: PromiseCreateArgs {
            method: "exit_to_near_precompile_callback".to_string(),
            ...
        },
    })
};
``` [4](#0-3) [5](#0-4) 

`ExitToEthereum` has no equivalent structure.

**Step 4 — Unchecked precompile return value compounds the risk.**

Because `EvmErc20.sol` never checks `res`, if the precompile itself returns `ExitError` (e.g., `ERR_TARGET_TOKEN_NOT_FOUND` when the ERC-20 is not registered in the NEP-141 map, or `ERR_KEY_NOT_FOUND` when the eth-connector account is unset), the `_burn` is already committed in EVM state, no NEAR promise is ever scheduled, and the tokens vanish with zero on-chain evidence of failure. [6](#0-5) 

### Impact Explanation

**Permanent freezing of funds.** Any ERC-20 tokens burned via `withdrawToEthereum` are irrecoverably destroyed if the NEAR-side `withdraw` call fails. The bridge's token accounting becomes insolvent: the EVM-side supply decreases but the Ethereum-side ETH is never released. Users who trigger this path lose their assets with no recourse. The existing test suite explicitly acknowledges the asymmetry between the two exit paths:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [7](#0-6) 

No analogous test exists for `ExitToEthereum` because there is no refund path to test.

### Likelihood Explanation

The NEAR-side `withdraw` promise can fail without any admin action:

- **Gas exhaustion**: The `WITHDRAWAL_GAS` constant is `100_000_000_000_000` (100 TGas). If the calling transaction does not have sufficient prepaid gas to cover both the EVM execution and the cross-contract call, the promise fails.
- **Eth-connector state**: If the eth-connector contract has insufficient NEP-141 balance (a bridge insolvency scenario analogous to the Holdefi report), the `withdraw` call fails.
- **Unregistered ERC-20**: If `withdrawToEthereum` is called on an ERC-20 whose address is not in the `Erc20Nep141Map`, the precompile returns `ExitError` immediately; the burn is committed but no promise is created. [8](#0-7) 

Any of these conditions can be triggered by an ordinary EVM user without privileged access.

### Recommendation

1. **Add an error-refund callback to `ExitToEthereum`** analogous to `exit_to_near_precompile_callback`. On NEAR-side `withdraw` failure, re-mint the burned ERC-20 tokens (or return ETH from the precompile address) to the original sender.

2. **Check the precompile call return value in `EvmErc20.sol` and `EvmErc20V2.sol`**. Replace the unchecked assembly block with a check that reverts if `res == 0`:
   ```solidity
   assembly {
       let res := call(...)
       if iszero(res) { revert(0, 0) }
   }
   ```
   This ensures the `_burn` is atomically rolled back if the precompile fails.

### Proof of Concept

1. Deploy an `EvmErc20` token on Aurora that is registered in the NEP-141 map.
2. Bridge tokens into Aurora (mint ERC-20 via `ft_on_transfer`).
3. Call `withdrawToEthereum(recipient, amount)` from an EVM account that holds tokens.
4. Arrange for the NEAR-side `withdraw` call to fail (e.g., by ensuring the calling transaction has insufficient prepaid gas, or by calling on an ERC-20 whose NEP-141 mapping has been removed).
5. Observe: the ERC-20 balance is reduced by `amount` (tokens burned), but no ETH is released on Ethereum and no refund occurs in the EVM. The tokens are permanently destroyed.

The structural proof is in the code: `ExitToEthereum::run()` always emits `PromiseArgs::Create` with no callback, [9](#0-8) 
while `ExitToNear::run()` conditionally wraps in `PromiseArgs::Callback` with a refund handler. [4](#0-3)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-76)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L66-77)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L42-62)
```rust
mod costs {
    use crate::prelude::types::{EthGas, NearGas};

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);

    /// Value determined experimentally based on tests and mainnet data. Example:
    /// `https://explorer.mainnet.near.org/transactions/5CD7NrqWpK3H8MAAU4mYEPuuWz9AqR9uJkkZJzw5b8PM#D1b5NVRrAsJKUX2ZGs3poKViu1Rgt4RJZXtTfMgdxH4S`
    pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);

    pub(super) const FT_TRANSFER_CALL_GAS: NearGas = NearGas::new(70_000_000_000_000);

    /// Value determined experimentally based on tests.
    pub(super) const EXIT_TO_NEAR_CALLBACK_GAS: NearGas = NearGas::new(10_000_000_000_000);

    // TODO(#332): Determine the correct amount of gas
    pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);
}
```

**File:** engine-precompiles/src/native.rs (L302-309)
```rust
fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
}
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

**File:** engine-precompiles/src/native.rs (L977-990)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
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
