### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Burn Without Bridge Transfer — (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens **before** invoking the exit precompile via inline assembly, but never check the `call` opcode's return value. If the precompile rejects the call for any reason (invalid recipient, oversized input, unregistered token), the burn is committed and the function returns successfully — permanently destroying the user's tokens with no corresponding NEAR or Ethereum transfer.

---

### Finding Description

In `EvmErc20.sol`, `withdrawToNear` executes:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is captured but NEVER checked — silent failure
    }
}
``` [1](#0-0) 

The identical pattern appears in `EvmErc20V2.sol`: [2](#0-1) 

And in both contracts' `withdrawToEthereum`: [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) performs several validations that can return `ExitError`, causing the EVM `call` opcode to return `0`:

- **Input size check**: `validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)` — rejects inputs shorter than 3 bytes or longer than 1,024 bytes. [5](#0-4) 
- **Recipient account ID parse**: `receiver_account_id.parse().map_err(...)` — rejects any string that is not a valid NEAR account ID. [6](#0-5) 
- **NEP-141 map lookup**: `get_nep141_from_erc20(erc20_address.as_bytes(), io)?` — rejects if the calling ERC-20 is not registered. [7](#0-6) 

When any of these fire, the precompile returns an `ExitError`. The EVM translates this to `call` returning `0`. Because the assembly block does not branch on `res`, execution falls through, the function returns without reverting, and the `_burn` is final.

---

### Impact Explanation

**Critical — Permanent freezing/destruction of user funds.**

The user's ERC-20 mirror tokens are irreversibly burned. No NEP-141 `ft_transfer` or Ethereum withdrawal is ever scheduled. The underlying bridged asset remains locked in the Aurora engine contract with no mechanism for the user to reclaim it. This is a one-way, unrecoverable loss.

---

### Likelihood Explanation

**Medium.** Any unprivileged token holder can trigger this by calling `withdrawToNear` with:

- A recipient string longer than ~991 bytes (exceeds `MAX_INPUT_SIZE = 1_024` after flag + amount bytes).
- A recipient string containing characters invalid for a NEAR account ID (e.g., uppercase letters, spaces, special characters).
- An empty recipient string (fails the minimum-length check).

These are realistic user mistakes. No admin access, private key compromise, or governance capture is required. The attacker-controlled entry path is the public `withdrawToNear(bytes memory recipient, uint256 amount)` function on any deployed `EvmErc20` or `EvmErc20V2` contract.

---

### Recommendation

Check the return value of the precompile `call` and revert on failure, **before** burning tokens, or at minimum revert after the failed call:

```solidity
// Option A: check res and revert
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Ideally, restructure the function to call the precompile **first** (in a view-like dry-run or with a revert guard), and only burn tokens after confirming the precompile accepted the call. This eliminates the burn-then-fail race entirely.

---

### Proof of Concept

1. Deploy `EvmErc20` for a registered NEP-141 token. Mint 100 tokens to `attacker`.
2. Call `withdrawToNear(bytes("INVALID ACCOUNT ID WITH SPACES"), 100)` from `attacker`.
3. `_burn(attacker, 100)` executes — attacker's balance drops to 0.
4. The precompile rejects the recipient (invalid NEAR account ID characters); `call` returns `0`.
5. Assembly does not check `res`; function returns `()` with no revert.
6. Transaction succeeds. Attacker has 0 ERC-20 tokens and receives 0 NEAR tokens. Funds are permanently lost.

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

**File:** engine-precompiles/src/native.rs (L373-378)
```rust
    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
```
