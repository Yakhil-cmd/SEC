### Title
Unchecked Precompile Return Value Causes Permanent ERC-20 Token Burn Without NEAR-Side Release — (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's EVM ERC-20 tokens **before** calling the exit precompile, and **never check** the return value of that precompile call. If the precompile fails for any reason, the EVM tokens are permanently destroyed while the corresponding NEP-141 tokens remain locked inside the Aurora contract with no recovery path. This is a direct ERC-20 mirror accounting bug: the EVM token supply decreases without the NEAR-side backing balance decreasing, permanently freezing the user's funds.

---

### Finding Description

In `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`, the execution order is:

1. `_burn(_msgSender(), amount)` — irreversibly destroys the caller's EVM tokens.
2. An inline `assembly` block calls the exit precompile (`0xe9217bc7...` or `0xb0bd02f6...`).
3. The return value `res` of the `call` opcode is stored in a local variable but **never checked or acted upon**.

`EvmErc20.sol` `withdrawToNear`:
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here, irreversibly

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — silent failure
    }
}
```

The same unchecked pattern appears in `withdrawToEthereum` in both contracts.

On the precompile side (`engine-precompiles/src/native.rs`), the `ExitToNear::run` function can return `Err(ExitError)` — causing the EVM `call` to return `res = 0` — in multiple reachable code paths:

- `parse_recipient` fails if the `recipient` bytes are not valid UTF-8 or do not form a valid NEAR account ID (e.g., uppercase letters, invalid characters, length > 64 bytes).
- `get_nep141_from_erc20` fails with `ERR_TARGET_TOKEN_NOT_FOUND` if the ERC-20 contract address has no registered NEP-141 mapping in storage.
- The `context.address != exit_to_near::ADDRESS.raw()` guard returns `ERR_INVALID_IN_DELEGATE` if the call context is wrong.

When the precompile returns an error, **no NEAR promise is scheduled**. The `error_refund` callback mechanism (lines 449–453 in `native.rs`) only fires as a NEAR-level callback after a successfully-scheduled `ft_transfer` promise fails on the NEAR side — it is never triggered when the precompile itself fails before scheduling any promise. The EVM transaction succeeds (no revert), the burn is final, and the NEP-141 tokens remain locked in the Aurora contract permanently.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

A user calling `withdrawToNear` or `withdrawToEthereum` with a recipient that fails precompile validation loses their EVM ERC-20 tokens entirely. The NEP-141 tokens that were deposited to mint those ERC-20 tokens remain locked in the Aurora contract with no mechanism to retrieve them: no promise was scheduled, no refund callback fires, and the EVM-side balance is already zero. The EVM token supply is now lower than the NEP-141 balance held by Aurora, creating a permanent accounting divergence (ERC-20 mirror insolvency for that user's position).

---

### Likelihood Explanation

**Moderate.** The `recipient` parameter in both contracts is typed as `bytes memory` with no Solidity-level validation before the burn. NEAR account IDs have strict format rules (lowercase alphanumeric, `_`, `-`, `.`; max 64 bytes; no leading/trailing separators). A user who accidentally passes an invalid account ID string (e.g., uppercase characters, an Ethereum address hex string, or a string exceeding 64 bytes) will trigger the failure path. This is a realistic user error, not a contrived scenario. The trigger requires no privileged access — any token holder can reach it.

---

### Recommendation

Reverse the operation order: call the exit precompile **first**, check its return value, and only call `_burn` if the precompile call succeeded. Alternatively, revert the transaction if `res == 0`:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    uint res;
    assembly {
        res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    }
    require(res == 1, "EvmErc20: exit precompile call failed");

    _burn(_msgSender(), amount);  // only burn after confirmed precompile success
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

**Entry path (no privileged access required):**

1. User holds 1000 units of a bridged ERC-20 token (`EvmErc20` instance) on Aurora.
2. User calls `withdrawToNear(bytes("INVALID ACCOUNT ID WITH SPACES"), 1000)`.
3. `_burn(msg.sender, 1000)` executes — user's EVM balance drops to 0, `totalSupply` decreases by 1000.
4. The precompile call is made. Inside `ExitToNear::run`, `parse_recipient` calls `str::from_utf8` (succeeds) then `.parse::<AccountId>()` on `"INVALID ACCOUNT ID WITH SPACES"` — this fails because NEAR account IDs cannot contain spaces. The precompile returns `Err(ExitError::Other("ERR_INVALID_RECEIVER_ACCOUNT_ID"))`.
5. The EVM `call` returns `res = 0`. The Solidity code does not check `res`. The outer transaction completes successfully.
6. No NEAR promise is scheduled. The NEP-141 tokens remain in the Aurora contract. The user's EVM tokens are gone. No refund callback is ever triggered.

**Result:** 1000 NEP-141 tokens are permanently locked in the Aurora contract. The EVM `totalSupply` for this token is now lower than the NEP-141 balance held by Aurora — a permanent accounting divergence and fund freeze.

**Affected files and lines:**

- `etc/eth-contracts/contracts/EvmErc20.sol` lines 53–63 (`withdrawToNear`) and 65–76 (`withdrawToEthereum`) — burn before unchecked precompile call. [1](#0-0) 

- `etc/eth-contracts/contracts/EvmErc20V2.sol` lines 53–64 (`withdrawToNear`) and 66–77 (`withdrawToEthereum`) — same pattern. [2](#0-1) 

- `engine-precompiles/src/native.rs` lines 359–379 (`parse_recipient`) — reachable failure path that returns `Err` for invalid NEAR account IDs. [3](#0-2) 

- `engine-precompiles/src/native.rs` lines 302–309 (`get_nep141_from_erc20`) — returns `Err` if ERC-20 has no NEP-141 mapping. [4](#0-3) 

- `engine-precompiles/src/native.rs` lines 449–453 — `error_refund` callback is only scheduled after a successful NEAR promise, not after a precompile-level failure. [5](#0-4)

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

**File:** engine-precompiles/src/native.rs (L359-379)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
        || (recipient, None),
        |(recipient, msg)| {
            if msg == UNWRAP_WNEAR_MSG {
                (recipient, Some(Message::UnwrapWnear))
            } else {
                (recipient, Some(Message::Omni(msg)))
            }
        },
    );

    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
}
```

**File:** engine-precompiles/src/native.rs (L449-453)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
```
