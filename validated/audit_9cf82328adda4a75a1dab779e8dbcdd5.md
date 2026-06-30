### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Silent Token Burn Without Withdrawal - (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.withdrawToNear` and `EvmErc20.withdrawToEthereum` burn the caller's ERC-20 tokens before calling the Aurora exit precompiles, but never check the return value of those `call` opcodes. If the precompile call fails for any reason, the tokens are permanently destroyed on Aurora while the corresponding NEP-141 or Ethereum-side transfer never occurs, resulting in permanent loss of user funds.

---

### Finding Description

In `etc/eth-contracts/contracts/EvmErc20.sol`, both withdrawal functions follow the same pattern:

1. Burn the caller's tokens unconditionally via `_burn`.
2. Construct the precompile input.
3. Issue a low-level `call` to the exit precompile address.
4. **Capture the return value `res` in a local variable but never check it.**

```solidity
// withdrawToNear (lines 53–63)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — silent failure
    }
}
``` [1](#0-0) 

```solidity
// withdrawToEthereum (lines 65–76)
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes20 recipient_b = bytes20(recipient);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
    uint input_size = 1 + 32 + 20;

    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — silent failure
    }
}
``` [2](#0-1) 

The `ExitToNear` precompile at `0xe9217bc7...` and the `ExitToEthereum` precompile at `0xb0bd02f6...` can fail for multiple reasons: an invalid/malformed NEAR recipient account ID, a missing NEP-141 mapping in storage, an amount exceeding `u128::MAX`, or any other error path that returns `ExitError`. [3](#0-2) 

When the precompile returns failure (EVM `call` returns 0), the Solidity code does not revert. The `_burn` has already executed and is not rolled back, so the user's ERC-20 balance is permanently zeroed while no NEAR-side `ft_transfer` or Ethereum-side `withdraw` promise is ever created. [4](#0-3) 

---

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

A user calling `withdrawToNear` or `withdrawToEthereum` with any input that causes the precompile to revert (e.g., a recipient string that fails `AccountId` parsing, or an amount that overflows `u128`) will have their ERC-20 tokens burned with no recourse. The tokens cease to exist on Aurora and are never credited on the destination chain. There is no refund path in the Solidity layer.

---

### Likelihood Explanation

**Medium.** Any unprivileged ERC-20 token holder can trigger this by calling `withdrawToNear` or `withdrawToEthereum` directly. The failure condition is reachable with a malformed recipient (e.g., a byte string that is not a valid NEAR account ID), which is a realistic user mistake. Additionally, any future precompile-level failure (storage corruption, missing connector account key) would silently drain all callers' tokens.

---

### Recommendation

Add a `require` check on the assembly `call` return value so that the entire transaction reverts if the precompile call fails, preventing the burn from taking effect:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum`. This ensures atomicity: either the burn and the exit both succeed, or neither does.

---

### Proof of Concept

1. Deploy `EvmErc20` on Aurora (or use an existing deployed instance).
2. Mint tokens to `address(attacker)`.
3. Call `withdrawToNear(bytes("not.a!!valid-near-account-id"), amount)` from `attacker`.
4. The `_burn` executes, reducing `attacker`'s balance to zero.
5. The precompile call fails because `"not.a!!valid-near-account-id"` fails `AccountId::try_from` inside `parse_recipient`. [5](#0-4) 
6. `res == 0` but is never checked; the function returns normally.
7. `attacker`'s ERC-20 balance is 0; no NEP-141 tokens are received on NEAR. Funds are permanently lost.

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

**File:** engine-precompiles/src/native.rs (L295-309)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
}

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

**File:** engine-precompiles/src/native.rs (L381-417)
```rust
impl<I: IO> Precompile for ExitToNear<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_NEAR_GAS)
    }

    #[allow(clippy::too_many_lines)]
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        is_static: bool,
    ) -> EvmPrecompileResult {
        // ETH (base) transfer input format: (85 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled
        //  - recipient_account_id (max MAX_INPUT_SIZE - 20 - 1 bytes)
        // ERC-20 transfer input format: (124 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled.
        //  - amount (32 bytes)
        //  - recipient_account_id (max MAX_INPUT_SIZE - 1 - (20) - 32 bytes)
        //  - `:unwrap` suffix in a case of wNEAR (7 bytes)
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
