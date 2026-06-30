### Title
Silo Mode ERC-20 Tokens Permanently Frozen When Transferred to Non-Whitelisted EVM Addresses - (File: engine/src/engine.rs)

### Summary

In Aurora Engine's Silo mode, the whitelist enforcement on `ft_on_transfer` (NEP-141 → ERC-20 bridge) redirects tokens away from non-whitelisted addresses via the `erc20_fallback_address` mechanism. However, no equivalent restriction exists for EVM-internal ERC-20 `transfer`/`transferFrom` calls. A whitelisted address can freely transfer ERC-20 tokens to any non-whitelisted EVM address. Because `assert_access` blocks all transaction submission from non-whitelisted addresses, the recipient can never call `exitToNear` or any other function to recover the tokens. The tokens are permanently frozen.

### Finding Description

Aurora Engine's Silo mode enforces two separate whitelist checks:

**1. Incoming bridge path (`ft_on_transfer`):** In `receive_erc20_tokens`, the engine checks `silo::is_allow_receive_erc20_tokens` and silently redirects tokens to `erc20_fallback_address` if the intended recipient is not whitelisted. [1](#0-0) 

**2. Transaction submission (`submit`):** `assert_access` enforces that both the NEAR predecessor account (`Account` whitelist) and the EVM address (`Address` whitelist) are whitelisted before any EVM transaction is executed. [2](#0-1) 

The gap is that **EVM-internal ERC-20 transfers** (standard `transfer`/`transferFrom` calls within the EVM) are not subject to any recipient whitelist check. A whitelisted address holding ERC-20 tokens can call the ERC-20 contract's `transfer` function to send tokens to an arbitrary, non-whitelisted EVM address. The ERC-20 contract executes inside the EVM without any silo whitelist enforcement on the recipient.

Once tokens arrive at a non-whitelisted address:
- The address cannot call `submit` (blocked by `assert_access` via `is_allow_submit`)
- Therefore it cannot call `exitToNear` (which requires submitting an EVM transaction to trigger the ERC-20 burn → precompile flow)
- No other party can exit tokens on behalf of the non-whitelisted address, because `exitToNear` for ERC-20 tokens burns from `msg.sender` (the transaction originator) [3](#0-2) 

The `is_allow_receive_erc20_tokens` function is only invoked in the `ft_on_transfer` bridge path, not in the EVM execution path for internal ERC-20 transfers. [4](#0-3) 

### Impact Explanation

**Permanent freezing of funds.** ERC-20 tokens (backed by NEP-141 assets) sent to a non-whitelisted EVM address in Silo mode are irrecoverable. The non-whitelisted address has no mechanism to submit any EVM transaction, so it cannot call `exitToNear` to redeem the underlying NEP-141 tokens. The underlying NEP-141 tokens remain locked in the Aurora engine contract with no path to withdrawal.

### Likelihood Explanation

Silo mode is an intentional production deployment configuration. Any whitelisted user who performs an ERC-20 `transfer` to a non-whitelisted address — whether by mistake, by interacting with a DeFi contract that routes tokens to arbitrary addresses, or by receiving tokens from a contract that does not check whitelist status — triggers this freeze. The scenario is realistic in any Silo deployment where ERC-20 tokens are used in composable contracts (e.g., AMMs, lending protocols) that may route tokens to addresses not pre-approved by the silo operator.

### Recommendation

Apply a recipient whitelist check inside the EVM execution path for ERC-20 token transfers, analogous to the existing `ft_on_transfer` fallback. One approach is to intercept ERC-20 `transfer`/`transferFrom` calls at the EVM level (e.g., via a precompile hook or by modifying the ERC-20 contract template deployed by `deploy_erc20_token`) and redirect tokens to `erc20_fallback_address` when the recipient is not whitelisted. Alternatively, document clearly that EVM-internal ERC-20 transfers to non-whitelisted addresses result in permanent fund loss, and enforce transfer restrictions at the ERC-20 contract level.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode: enable `Address` whitelist, set `erc20_fallback_address`, deploy a NEP-141 token and its ERC-20 mirror.
2. Whitelist address `A`. Do not whitelist address `B`.
3. Transfer NEP-141 tokens to Aurora via `ft_transfer_call` with `msg = A.encode()`. Tokens correctly mint to `A` (whitelisted).
4. From `A`, submit an EVM transaction calling `erc20.transfer(B, amount)`. This succeeds because `A` is whitelisted and `assert_access` only checks the sender.
5. Observe that `B` now holds ERC-20 tokens.
6. Attempt to submit any EVM transaction from `B` (e.g., `exitToNear`). The call is rejected with `EngineErrorKind::NotAllowed` because `B` is not in the `Address` whitelist.
7. The tokens at `B` are permanently frozen with no recovery path. [5](#0-4) [6](#0-5)

### Citations

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```

**File:** engine/src/engine.rs (L1756-1775)
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

    if !allowed {
        return Err(EngineError {
            kind: EngineErrorKind::NotAllowed,
            gas_used: 0,
        });
    }

    Ok(())
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-143)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L155-158)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
```
