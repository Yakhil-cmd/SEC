### Title
Unchecked Return Value of Exit Precompile `call()` After Irreversible Token Burn - (`File: etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear()` and `withdrawToEthereum()` functions first irreversibly burn the caller's ERC-20 tokens, then invoke the Aurora exit precompile via a low-level assembly `call()`. The return value (`res`) of that `call()` is captured but **never checked**. If the precompile call fails for any reason (e.g., malformed recipient, insufficient gas, or any internal precompile error), the tokens are permanently destroyed with no corresponding NEP-141 or Ethereum-side transfer occurring.

---

### Finding Description

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement the same pattern in `withdrawToNear` and `withdrawToEthereum`:

**`EvmErc20.sol` — `withdrawToNear` (lines 53–63):**
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // <-- irreversible burn

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
```

**`EvmErc20.sol` — `withdrawToEthereum` (lines 65–76):**
```solidity
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // <-- irreversible burn
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
```

`EvmErc20V2.sol` contains the identical unchecked pattern in both functions (lines 53–63 and 66–77).

The exit precompile (`ExitToNear` at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`, `ExitToEthereum` at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) can return failure (i.e., `call()` returns `0`) under multiple conditions visible in the precompile's Rust implementation. For `withdrawToNear`, the `recipient` parameter is a raw `bytes` value supplied by the caller; if it does not parse as a valid NEAR account ID, the precompile returns an `ExitError`. For `withdrawToEthereum`, similar parsing and validation errors can occur. In all failure cases, the Solidity function does not revert — it silently returns success — while the tokens have already been burned.

---

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

The `_burn()` call is unconditional and executes before the precompile call. If the precompile call fails:
- The user's ERC-20 balance is permanently reduced to zero for the withdrawn amount.
- No NEP-141 tokens are transferred to the NEAR recipient (for `withdrawToNear`).
- No ETH is released on Ethereum (for `withdrawToEthereum`).
- There is no refund mechanism; the tokens are gone.

This constitutes direct, permanent loss of user funds.

---

### Likelihood Explanation

**Medium-High.**

The `recipient` parameter in `withdrawToNear` is a caller-supplied `bytes` value with no on-chain validation before the burn. A user who passes a malformed or invalid NEAR account ID (e.g., an account ID that is too long, contains invalid characters, or is otherwise rejected by the precompile's parser) will trigger a silent precompile failure. Additionally, any future precompile-level error condition (e.g., gas exhaustion within the precompile, state inconsistency) would silently cause the same outcome. The function is externally callable by any token holder with no access restriction.

---

### Recommendation

Add a `require(res != 0, "ERR_EXIT_PRECOMPILE_FAILED")` check immediately after each assembly `call()` block in both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. This ensures the transaction reverts if the precompile call fails, preventing the burn from taking effect. Alternatively, restructure the logic so the precompile call is attempted first (in a view/static context if possible) and the burn only occurs after confirmed success.

---

### Proof of Concept

1. Deploy `EvmErc20` (or use the existing bridged token).
2. Mint tokens to an attacker-controlled address.
3. Call `withdrawToNear(invalidRecipient, amount)` where `invalidRecipient` is a bytes value that fails NEAR account ID validation (e.g., `bytes("!!invalid!!")` or an empty bytes value).
4. The `_burn()` executes, reducing the caller's balance by `amount`.
5. The precompile call at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` returns `0` (failure) because the recipient is invalid — confirmed by the `ExitToNearParams::try_from` parsing logic in `engine-precompiles/src/native.rs` which returns `ExitError` on malformed input.
6. The assembly block captures `res = 0` but does not revert.
7. The function returns successfully. The caller's tokens are permanently destroyed; no NEP-141 transfer occurred.

**Affected files and lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** engine-precompiles/src/native.rs (L727-775)
```rust
impl<'a> TryFrom<&'a [u8]> for ExitToNearParams<'a> {
    type Error = ExitError;

    fn try_from(input: &'a [u8]) -> Result<Self, Self::Error> {
        // The first byte of the input is a flag, selecting the behavior to be triggered:
        // 0x00 -> Eth(base) token withdrawal
        // 0x01 -> ERC-20 token withdrawal
        let flag = input
            .first()
            .copied()
            .ok_or_else(|| ExitError::Other(Cow::from("ERR_MISSING_FLAG")))?;

        #[cfg(feature = "error_refund")]
        let (refund_address, input) = parse_input(input)?;
        #[cfg(not(feature = "error_refund"))]
        let input = parse_input(input)?;

        match flag {
            0x0 => {
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(input)?;

                Ok(Self::BaseToken(BaseTokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    message,
                }))
            }
            0x1 => {
                let amount = parse_amount(&input[..32])?;
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(&input[32..])?;

                Ok(Self::Erc20TokenParams(Erc20TokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    amount,
                    message,
                }))
            }
            _ => Err(ExitError::Other(Cow::from("ERR_INVALID_FLAG"))),
        }
    }
```
