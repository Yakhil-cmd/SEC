### Title
Unchecked Precompile Call Return Value in `EvmErc20.withdrawToNear` / `withdrawToEthereum` Causes Permanent Fund Freeze — (`File: etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.withdrawToNear` and `EvmErc20.withdrawToEthereum` burn the caller's ERC-20 tokens and then invoke the Aurora exit precompile via inline assembly. The return value of the precompile `call` is captured in a local variable `res` but is **never checked**. If the precompile call fails for any reason, the burn is not reverted, the tokens are permanently destroyed, and no corresponding NEP-141 or Ethereum asset is released. This is a direct analog to the reference bug: accounting state (token supply) is updated without the corresponding asset transfer being verified or enforced.

---

### Finding Description

In `etc/eth-contracts/contracts/EvmErc20.sol`:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no require(res != 0)
    }
}
```

The same pattern appears in `withdrawToEthereum` (line 65–76) and identically in `EvmErc20V2.sol` (lines 53–77).

The `ExitToNear` precompile (`0xe921…`) can fail and return `0` in several reachable conditions:

1. **Invalid NEAR account ID in `recipient`**: The precompile parses `recipient` as a NEAR `AccountId`. If the bytes are not a valid account ID (e.g., too long, contains illegal characters, or is empty), `ExitToNearParams::try_from(input)` returns an `ExitError`, the precompile returns failure, `res = 0`, but the Solidity function does not revert.
2. **Insufficient gas forwarded**: The precompile requires `costs::EXIT_TO_NEAR_GAS`. If the remaining gas at the point of the assembly `call` is below this threshold, the precompile fails with `OutOfGas`.
3. **ERC-20 not registered in NEP-141 mapping**: `get_nep141_from_erc20` inside `exit_erc20_token_to_near` returns an error if the ERC-20 address has no registered NEP-141 counterpart.

In all cases, `_burn` has already executed and is not rolled back. The EVM transaction succeeds (no revert), the token supply is permanently reduced, and no NEAR or Ethereum assets are released to the recipient.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any holder of a bridged `EvmErc20` token who calls `withdrawToNear` or `withdrawToEthereum` under conditions that cause the precompile to fail will have their tokens permanently destroyed with zero compensation. The tokens are removed from the EVM supply (burned) but the corresponding NEP-141 tokens held by the Aurora contract are never released. The bridge accounting becomes permanently insolvent for that amount: Aurora holds NEP-141 tokens that can never be claimed.

---

### Likelihood Explanation

**Medium.**

The most realistic trigger is a user supplying a malformed `recipient` bytes argument (e.g., an empty byte array, a string exceeding 64 characters, or bytes containing characters illegal in NEAR account IDs). The `withdrawToNear` function accepts a raw `bytes memory recipient` with no Solidity-level validation before the burn. A user who mistypes or programmatically constructs an invalid recipient will silently lose their tokens. This is reachable by any unprivileged EVM user who holds `EvmErc20` tokens and calls the function directly or through a wrapper contract.

---

### Recommendation

Add a `require` check on the precompile call return value in both `withdrawToNear` and `withdrawToEthereum`, and in both `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures that if the precompile fails, the entire transaction reverts, the `_burn` is rolled back, and the user's tokens are preserved.

---

### Proof of Concept

1. Deploy an `EvmErc20` token via the Aurora bridge (standard flow).
2. Acquire a balance of the token as an EVM user.
3. Call `withdrawToNear` with a `recipient` that is an invalid NEAR account ID (e.g., `bytes("")` — empty bytes, which the precompile will reject as an invalid `AccountId`).
4. Observe: the transaction succeeds (no revert), the caller's token balance is zero (burned), the NEP-141 balance of the Aurora contract is unchanged, and the caller receives nothing on NEAR.
5. The burned amount is permanently unrecoverable: no refund callback is triggered because no promise was ever created (the precompile failed before scheduling any cross-contract call). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** engine-precompiles/src/native.rs (L404-417)
```rust
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }
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
