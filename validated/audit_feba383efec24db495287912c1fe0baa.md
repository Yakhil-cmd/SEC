### Title
Silo ERC-20 Whitelist Bypass via EVM-Internal Transfer Causes Token Freeze - (File: `engine/src/engine.rs`)

### Summary

In Silo mode, the `is_allow_receive_erc20_tokens` restriction is enforced only at the `ft_on_transfer` bridge entry point. A whitelisted EVM address can bypass this restriction entirely by calling the standard ERC-20 `transfer()` function inside the EVM, routing tokens to any non-whitelisted address. Because non-whitelisted addresses cannot submit transactions, those tokens become frozen with no self-service recovery path.

### Finding Description

Aurora Engine's Silo mode enforces an `Address` whitelist to control who may receive ERC-20 tokens. The check is implemented in `receive_erc20_tokens` inside `engine/src/engine.rs`:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

This guard redirects tokens to the `erc20_fallback_address` when the intended recipient is not whitelisted. However, this check is **only applied during the `ft_on_transfer` bridge flow**. Once tokens are minted to a whitelisted address, the underlying ERC-20 contract (`EvmErc20.sol`) is a standard OpenZeppelin token with no whitelist enforcement on its `transfer()` function:

```solidity
function mint(address account, uint256 amount) public onlyAdmin {
    _mint(account, amount);
}
// inherits standard ERC20.transfer() with no whitelist check
``` [2](#0-1) 

A whitelisted address can therefore call `transfer()` on the ERC-20 contract to send tokens to any arbitrary address, including non-whitelisted ones, with no silo restriction applied.

The `is_allow_receive_erc20_tokens` function itself only checks the `Address` whitelist:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
``` [3](#0-2) 

But this function is never consulted during EVM-internal execution. The `assert_access` function that gates transaction submission checks the **sender**, not the recipient:

```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };
``` [4](#0-3) 

Once a non-whitelisted address holds ERC-20 tokens, it cannot submit any transaction (including `withdrawToNear`) because `is_allow_submit` will reject it. The `withdrawToNear` function in `EvmErc20.sol` is only reachable via an EVM transaction:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    // calls exit_to_near precompile
``` [5](#0-4) 

### Impact Explanation

ERC-20 tokens transferred to a non-whitelisted address via EVM `transfer()` are permanently inaccessible to that address without admin intervention. The non-whitelisted address cannot submit any transaction — including `withdrawToNear` or any ERC-20 `transfer()` — because `assert_access` blocks all submissions from non-whitelisted senders. This constitutes a **temporary freeze of funds** (requiring admin to whitelist the address to recover), matching the **High** impact tier.

### Likelihood Explanation

The scenario requires a whitelisted address (both `Account` and `Address` whitelists satisfied) to send ERC-20 tokens to a non-whitelisted address. This can occur:
- Accidentally, when a whitelisted user sends tokens to an address they do not know is non-whitelisted.
- Deliberately, as a griefing vector against a victim whose address is not in the silo whitelist.

Silo mode is an explicitly deployed configuration used by real Aurora-based networks. The `Address` whitelist is enabled by operators who intend to restrict token receipt. The bypass is reachable by any whitelisted participant with no special privileges beyond normal whitelist membership.

### Recommendation

Enforce the `is_allow_receive_erc20_tokens` check at the ERC-20 contract level by overriding the `_beforeTokenTransfer` (or `_update` in OpenZeppelin v5) hook in `EvmErc20.sol` to call the silo whitelist precompile, or alternatively add a recipient whitelist check inside the EVM execution path for ERC-20 transfers in silo mode. At minimum, document that EVM-internal ERC-20 transfers are not subject to the silo whitelist, so operators understand the gap.

### Proof of Concept

1. Silo mode is active: `set_silo_params` has been called with a valid `erc20_fallback_address`, and the `Address` whitelist is enabled.
2. Address `W` is in the `Address` whitelist; address `N` is not.
3. `W` receives 1000 ERC-20 tokens via `ft_on_transfer` — the whitelist check passes and tokens are minted to `W`.
4. `W` submits an EVM transaction calling `erc20.transfer(N, 1000)`. `assert_access` passes because `W` is whitelisted as the sender. The ERC-20 `transfer()` executes with no silo check on the recipient.
5. `N` now holds 1000 tokens. Any attempt by `N` to submit a transaction (e.g., `erc20.transfer(...)`, `withdrawToNear(...)`) is rejected by `assert_access` with `EngineErrorKind::NotAllowed`.
6. The 1000 tokens are frozen at `N` until the silo admin adds `N` to the `Address` whitelist.

### Citations

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```

**File:** engine/src/engine.rs (L1756-1765)
```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

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

**File:** engine/src/contract_methods/silo/mod.rs (L140-143)
```rust
/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```
