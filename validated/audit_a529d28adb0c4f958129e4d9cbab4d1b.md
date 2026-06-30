### Title
Unchecked Precompile `call` Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear()` and `withdrawToEthereum()` by first burning the caller's ERC-20 tokens and then calling the exit precompile via inline assembly. The return value `res` of the low-level `call` is captured but **never checked**. If the precompile call fails at the EVM level, the burn is committed and the tokens are permanently destroyed with no corresponding NEP-141 release, causing irreversible fund loss.

---

### Finding Description

In `EvmErc20.sol`, `withdrawToNear` is implemented as:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — silent failure
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum` and in both functions of `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) [4](#0-3) 

The target precompile addresses are `ExitToNear` at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` and `ExitToEthereum` at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`. [5](#0-4) 

Both precompiles can return `Err(ExitError::...)` — causing the EVM `call` to return `res = 0` — under several conditions:

1. **Out of gas**: `required_gas > target_gas` causes `return Err(ExitError::OutOfGas)`. [6](#0-5) 

2. **Token not registered**: `get_nep141_from_erc20` returns `Err` if the ERC-20 → NEP-141 mapping is absent from storage. [7](#0-6) 

3. **Invalid input / flag**: parsing failures propagate as `ExitError::Other(...)`. [8](#0-7) 

In standard EVM semantics, a failed low-level `call` returns `0` in `res` but does **not** revert the caller's prior state changes. Because `_burn` executes before the `call`, the token supply reduction is committed regardless of whether the precompile succeeds. The function then returns normally, leaving the user with no tokens and no withdrawal.

The `error_refund` feature provides a NEAR-side refund only when the downstream `ft_transfer` NEAR promise fails — it does not cover the case where the precompile itself fails at the EVM level before any promise is scheduled. [9](#0-8) [10](#0-9) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the precompile call fails silently:
- The user's ERC-20 bridge tokens are burned (supply reduced, balance zeroed).
- No NEAR promise is scheduled, so the corresponding NEP-141 tokens held by the Aurora contract are never released.
- The user loses the full `amount` with no recovery path.

This matches the "permanent freezing of funds" impact class.

---

### Likelihood Explanation

**Medium.** The most realistic trigger is an out-of-gas condition: a user or integrating contract that calls `withdrawToNear`/`withdrawToEthereum` with a gas limit that is sufficient for `_burn` but insufficient for the precompile's `EXIT_TO_NEAR_GAS` / `EXIT_TO_ETHEREUM_GAS` cost. A secondary trigger is any future state where the NEP-141 mapping for a given ERC-20 is absent (e.g., storage migration edge case), which would cause `get_nep141_from_erc20` to fail. Both conditions are reachable by an unprivileged EVM user without any admin involvement.

---

### Recommendation

Add an explicit revert inside the assembly block if the `call` returns `0`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply this fix to all four assembly blocks across `EvmErc20.sol` and `EvmErc20V2.sol`. This ensures that if the precompile fails, the entire transaction reverts (including the `_burn`), preserving the user's token balance.

---

### Proof of Concept

1. Deploy `EvmErc20` (or `EvmErc20V2`) with a valid NEP-141 mapping.
2. Mint tokens to `alice`.
3. `alice` calls `withdrawToNear(recipient, amount)` with a gas limit that covers `_burn` but falls below `EXIT_TO_NEAR_GAS` for the precompile.
4. The precompile returns `ExitError::OutOfGas`; the EVM `call` returns `res = 0`.
5. The assembly block exits without reverting; `withdrawToNear` returns successfully.
6. Observe: `alice`'s ERC-20 balance is zero; no NEP-141 tokens were transferred to `recipient`; the NEP-141 balance of the Aurora contract is unchanged.
7. `alice`'s funds are permanently lost. [1](#0-0) [11](#0-10)

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
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

**File:** engine-precompiles/src/native.rs (L270-278)
```rust
pub mod exit_to_near {
    use crate::prelude::types::{Address, make_address};

    /// Exit to NEAR precompile address
    ///
    /// Address: `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`
    /// This address is computed as: `&keccak("exitToNear")[12..]`
    pub const ADDRESS: Address = make_address(0xe9217bc7, 0x0b7ed1f598ddd3199e80b093fa71124f);
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

**File:** engine-precompiles/src/native.rs (L404-410)
```rust
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }
```

**File:** engine-precompiles/src/native.rs (L413-417)
```rust
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
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
