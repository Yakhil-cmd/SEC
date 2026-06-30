### Title
Unchecked Precompile Return Value in `withdrawToNear` Allows Permanent Fund Freeze - (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

### Summary

`EvmErc20.sol` burns ERC-20 tokens before calling the `ExitToNear` precompile via a low-level assembly `call`. The return value of that call is captured in `res` but never checked. If the precompile call fails for any reason (e.g., invalid NEAR account ID supplied as `recipient`), the ERC-20 tokens are permanently destroyed while the corresponding NEP-141 tokens remain locked inside the Aurora engine contract on NEAR — a permanent, irrecoverable fund freeze.

### Finding Description

`withdrawToNear` in `EvmErc20.sol` follows this sequence:

1. `_burn(_msgSender(), amount)` — irreversibly destroys the caller's ERC-20 tokens.
2. Constructs calldata and invokes the `ExitToNear` precompile (`0xe9217bc7…`) via an inline assembly `call`.
3. The return value `res` is stored but **never inspected**; no `require(res != 0)` or equivalent guard exists. [1](#0-0) 

Because `_burn` executes before the precompile call, and because a low-level EVM `call` does **not** revert the caller on failure, any failure of the precompile is silently swallowed. The ERC-20 balance is already zeroed; no NEAR promise is ever scheduled to release the NEP-141 tokens held by the engine.

The `ExitToNear` precompile (`exit_erc20_token_to_near`) can fail when:
- The `recipient` bytes do not parse as a valid NEAR account ID (e.g., empty bytes, bytes containing invalid characters, or a byte sequence exceeding 64 characters).
- The ERC-20 address is not found in the `Erc20Nep141Map` (e.g., a non-standard deployment path). [2](#0-1) 

The `nep141_erc20_map` (a `BijectionMap`) is the secondary state that must be consistent with the ERC-20 burn: burning ERC-20 tokens is the primary operation, and releasing NEP-141 tokens via the precompile is the mandatory secondary update. When the secondary update silently fails, the two sides of the bridge become permanently inconsistent — exactly the same structural pattern as the NFTPool `ownerToId` mapping not being updated during ERC-721 transfers. [3](#0-2) [4](#0-3) 

The same unchecked-return pattern exists in `withdrawToEthereum`, though the fixed-size Ethereum address recipient makes invalid-input failure less likely there. [5](#0-4) 

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the precompile call fails silently:
- The caller's ERC-20 tokens are burned and unrecoverable on the Aurora side.
- The NEP-141 tokens remain locked inside the Aurora engine contract on NEAR with no mechanism to release them.
- There is no recovery path: the bridge accounting is permanently broken for that amount.

### Likelihood Explanation

**Medium.** The `recipient` parameter of `withdrawToNear` is typed as `bytes memory`, not `string` or `address`. Any caller — including a smart contract integrating with `EvmErc20` — can supply bytes that do not constitute a valid NEAR account ID (e.g., an empty array, raw binary data, or an oversized payload). This is a realistic mistake for integrators unfamiliar with NEAR account ID encoding rules. No privileged access is required; any token holder can trigger the condition.

### Recommendation

Add a return-value check immediately after each assembly `call` and revert the entire transaction if the precompile signals failure. This ensures `_burn` is only committed when the exit is guaranteed to succeed:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        if iszero(res) { revert(0, 0) }
    }
}
```

Apply the same fix to `withdrawToEthereum`. Alternatively, restructure the function to call the precompile first and only burn on confirmed success.

### Proof of Concept

1. Deploy or use an existing `EvmErc20` instance on Aurora.
2. Call `withdrawToNear` with `recipient = ""` (empty bytes) and any non-zero `amount` the caller holds.
3. Observe: the transaction succeeds (no revert), the caller's ERC-20 balance is reduced by `amount`, but no NEP-141 tokens are released on NEAR — the `ExitToNear` precompile rejected the empty account ID silently.
4. The NEP-141 tokens remain permanently locked in the Aurora engine contract with no recovery mechanism.

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

**File:** engine/src/map.rs (L6-35)
```rust
/// A map storing a 1:1 relation between elements of types L and R.
/// The map is backed by storage of type I.
pub struct BijectionMap<L, R, I> {
    left_prefix: KeyPrefix,
    right_prefix: KeyPrefix,
    io: I,
    left_phantom: PhantomData<L>,
    right_phantom: PhantomData<R>,
}

impl<L: AsRef<[u8]> + TryFrom<Vec<u8>>, R: AsRef<[u8]> + TryFrom<Vec<u8>>, I: IO>
    BijectionMap<L, R, I>
{
    pub const fn new(left_prefix: KeyPrefix, right_prefix: KeyPrefix, io: I) -> Self {
        Self {
            left_prefix,
            right_prefix,
            io,
            left_phantom: PhantomData,
            right_phantom: PhantomData,
        }
    }

    pub fn insert(&mut self, left: &L, right: &R) {
        let key = self.left_key(left);
        self.io.write_storage(&key, right.as_ref());

        let key = self.right_key(right);
        self.io.write_storage(&key, left.as_ref());
    }
```

**File:** engine/src/engine.rs (L1493-1496)
```rust
#[must_use]
pub const fn nep141_erc20_map<I: IO>(io: I) -> BijectionMap<NEP141Account, ERC20Address, I> {
    BijectionMap::new(KeyPrefix::Nep141Erc20Map, KeyPrefix::Erc20Nep141Map, io)
}
```
