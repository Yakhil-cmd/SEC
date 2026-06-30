### Title
ERC-20 Tokens Permanently Burned When Exit Precompile Call Fails Due to Unchecked Return Value - (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions burn ERC-20 tokens **before** calling the exit precompile via a low-level assembly `call`. The return value of that call is **never checked**. If the precompile fails for any reason — including an oversized recipient input exceeding `MAX_INPUT_SIZE = 1024` bytes, an invalid NEAR account ID, or a paused precompile — the tokens are permanently destroyed with no corresponding transfer on NEAR or Ethereum.

---

### Finding Description

In `EvmErc20.sol`, `withdrawToNear` follows this sequence:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // (1) tokens destroyed

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // (2) res is NEVER checked
    }
}
``` [1](#0-0) 

The same pattern exists in `EvmErc20V2.sol`: [2](#0-1) 

And in `withdrawToEthereum` in both contracts: [3](#0-2) 

The `ExitToNear` precompile enforces a hard `MAX_INPUT_SIZE = 1024` bytes: [4](#0-3) 

Input validation happens inside `parse_input`, called before any state change in the precompile: [5](#0-4) 

For `EvmErc20.sol`, the precompile input layout is `flag(1) + amount(32) + recipient(variable)`. If `recipient.length > 991`, the precompile returns `ExitError`, causing the EVM `call` opcode to return `0`. Because `res` is never inspected, the Solidity function returns normally — but the `_burn` is **not** reverted. The ERC-20 tokens are gone.

The same failure can occur if:
- The `ExitToNear` precompile is paused via `PausePrecompiles` (a legitimate admin operation)
- The NEP-141 ↔ ERC-20 mapping is absent (`get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND`)
- The eth-connector account key is missing [6](#0-5) 

The analog to the original report is direct: in the Derby report, `deltaAllocationProtocol` is zeroed out **before** the vault's blacklist check reverts, making the state unrecoverable. Here, `_burn` destroys tokens **before** the precompile validates input, and the failure is silently swallowed.

---

### Impact Explanation

**Critical — Permanent freezing of funds.** ERC-20 tokens are irreversibly burned. No corresponding NEP-141 tokens are transferred to NEAR. The user's funds are unrecoverable. This affects every bridged ERC-20 token deployed by the Aurora Engine.

---

### Likelihood Explanation

**Medium.** Any unprivileged EVM user holding bridged ERC-20 tokens can trigger this by calling `withdrawToNear` with a `recipient` byte array longer than 991 bytes. No special permissions are required. Additionally, if an admin legitimately pauses the `ExitToNear` precompile for maintenance, every subsequent `withdrawToNear` call silently burns tokens until the precompile is resumed.

---

### Recommendation

Check the return value of the precompile `call` and revert on failure, so that `_burn` is rolled back atomically:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

1. Acquire any bridged ERC-20 token on Aurora (e.g., via `ft_transfer_call` from NEAR).
2. Call `withdrawToNear(recipient, amount)` where `recipient` is a byte array of length ≥ 992 bytes.
3. `_burn(_msgSender(), amount)` executes — ERC-20 balance decreases permanently.
4. The precompile receives input of size `1 + 32 + 992 = 1025 > 1024`; `validate_input_size` returns `Err("ERR_INVALID_INPUT")`.
5. The EVM `call` returns `0`; `res` is never read; the Solidity function returns without reverting.
6. The user's ERC-20 tokens are permanently destroyed. No NEP-141 tokens arrive on NEAR. [1](#0-0) [4](#0-3) [7](#0-6)

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

**File:** engine-precompiles/src/native.rs (L37-40)
```rust
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
const MAX_INPUT_SIZE: usize = 1_024;
```

**File:** engine-precompiles/src/native.rs (L295-300)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
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

**File:** engine-precompiles/src/native.rs (L787-791)
```rust
#[cfg(not(feature = "error_refund"))]
fn parse_input(input: &[u8]) -> Result<&[u8], ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    Ok(&input[1..])
}
```
