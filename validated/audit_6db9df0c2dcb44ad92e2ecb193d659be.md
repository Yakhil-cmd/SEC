### Title
Unchecked Precompile Call Return Value After Token Burn Causes Permanent Fund Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear` and `withdrawToEthereum` by first burning the caller's tokens via `_burn`, then invoking the Aurora exit precompile via a low-level assembly `call`. The return value of that assembly `call` (indicating precompile success or failure) is captured in a local variable `res` but is **never checked**. If the precompile call fails for any reason, execution continues silently, the tokens remain permanently destroyed, and no withdrawal is ever initiated.

---

### Finding Description

In `EvmErc20.sol`, both withdrawal functions follow this pattern:

```solidity
// withdrawToNear (lines 53–63)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no revert if res == 0
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum`: [2](#0-1) 

And both functions are duplicated verbatim in `EvmErc20V2.sol`: [3](#0-2) [4](#0-3) 

The exit precompile (`ExitToNear` at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`, `ExitToEthereum` at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) has multiple documented failure paths in `engine-precompiles/src/native.rs`:

1. **`get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND`** — if the ERC-20 contract address is not registered in the `Erc20Nep141Map` storage: [5](#0-4) 

2. **`get_eth_connector_contract_account` returns `ERR_KEY_NOT_FOUND`** — if the eth connector account key is absent from storage: [6](#0-5) 

3. **`validate_input_size` fails** — if the encoded input does not satisfy the size bounds: [7](#0-6) 

4. **`parse_amount` fails** — if the amount exceeds `u128::MAX`: [8](#0-7) 

In every one of these failure cases the precompile returns an `ExitError`, which causes the EVM `call` opcode to return `0`. Because the Solidity assembly block never checks `res`, the outer function does not revert. The `_burn` that already executed is not rolled back, and the user's tokens are gone with no corresponding withdrawal promise created.

---

### Impact Explanation

**Critical — Permanent freezing/destruction of user funds.**

A user who calls `withdrawToNear` or `withdrawToEthereum` on any `EvmErc20`/`EvmErc20V2` token under conditions that cause the precompile to fail will have their ERC-20 balance permanently destroyed. No NEAR-side `ft_transfer` or Ethereum-side `withdraw` promise is ever scheduled. The tokens cannot be recovered because the burn is irreversible and no refund path exists in the Solidity contract.

---

### Likelihood Explanation

**Medium.** The most realistic trigger is an ERC-20 token whose address is not present in the `Erc20Nep141Map` storage (e.g., a token deployed via a non-standard path, or one whose registration was never completed). Any holder of such a token who attempts a withdrawal will silently lose their entire withdrawn amount. Additionally, any future storage migration or connector reconfiguration that temporarily removes the `EthConnectorAccount` key would expose all `EvmErc20` token holders to the same silent-burn outcome. The call path is fully unprivileged — any token holder can trigger it.

---

### Recommendation

Restructure both `withdrawToNear` and `withdrawToEthereum` in `EvmErc20.sol` and `EvmErc20V2.sol` to check the return value of the assembly `call` and revert if it is zero, **before** burning tokens. The safest pattern is to perform the precompile call first (in a read-only or non-destructive way) and only burn on confirmed success, or at minimum to revert the entire transaction when `res == 0`:

```solidity
assembly {
    let res := call(gas(), EXIT_PRECOMPILE_ADDRESS, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Ideally, move `_burn` to **after** the precompile call succeeds, so that a failed precompile never results in a burned balance.

---

### Proof of Concept

1. Deploy an `EvmErc20` token whose ERC-20 address is **not** registered in the Aurora engine's `Erc20Nep141Map` (e.g., a token minted directly without going through `deploy_erc20_token`).
2. Mint tokens to `alice` via the admin `mint` function.
3. `alice` calls `withdrawToNear(recipient_bytes, amount)`.
4. `_burn(alice, amount)` executes — alice's balance drops to zero.
5. The assembly `call` to `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` fails because `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` (storage key absent). [9](#0-8) 

6. The EVM `call` returns `res = 0`. The assembly block does not check `res`. The Solidity function returns normally.
7. `alice`'s tokens are permanently destroyed. No NEAR-side withdrawal promise was created. Funds are irrecoverably lost.

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

**File:** engine-precompiles/src/native.rs (L311-319)
```rust
fn get_eth_connector_contract_account<I: IO>(io: &I) -> Result<AccountId, ExitError> {
    io.read_storage(&construct_contract_key(
        EthConnectorStorageId::EthConnectorAccount,
    ))
    .ok_or(ExitError::Other(Cow::Borrowed("ERR_KEY_NOT_FOUND")))
    .and_then(|x| {
        x.to_value()
            .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_DESERIALIZE")))
    })
```

**File:** engine-precompiles/src/native.rs (L337-344)
```rust
fn parse_amount(input: &[u8]) -> Result<U256, ExitError> {
    let amount = U256::from_big_endian(input);

    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }

    Ok(amount)
```

**File:** engine-precompiles/src/native.rs (L583-583)
```rust
    let nep141_account_id = get_nep141_from_erc20(erc20_address.as_bytes(), io)?;
```
