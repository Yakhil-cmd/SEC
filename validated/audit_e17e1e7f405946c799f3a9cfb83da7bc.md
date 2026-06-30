### Title
Legacy ERC-20 Mirror Deployment Hardcodes `decimals: 0`, Breaking Token Accounting for All Non-Zero Decimal NEP-141 Tokens - (File: `engine/src/engine.rs`, `engine-types/src/parameters/connector.rs`)

---

### Summary

When a NEP-141 token is bridged to Aurora using the legacy `deploy_erc20_token` path, the resulting ERC-20 mirror is deployed with `decimals: 0` (from `Erc20Metadata::default()`), regardless of the actual NEP-141 token's decimal precision. Any EVM-side DeFi protocol on Aurora that reads `decimals()` from such a mirror will receive `0` instead of the true value (e.g., 6 for USDC, 18 for most tokens), causing catastrophic misvaluation of token amounts and enabling fund theft from those protocols.

---

### Finding Description

**Root cause — `Erc20Metadata::default()` sets `decimals: 0`:** [1](#0-0) 

**`setup_deploy_erc20_input` silently falls back to this default when no metadata is supplied:** [2](#0-1) 

**The legacy `deploy_erc20_token` call path always passes `None` as metadata:** [3](#0-2) 

**`DeployErc20TokenArgs::deserialize` falls back to `Legacy` for any raw `AccountId` bytes, meaning all pre-existing callers and any caller that does not use the newer `WithMetadata` variant will silently hit this path:** [4](#0-3) 

The `WithMetadata` variant correctly fetches `ft_metadata` from the NEP-141 contract and propagates the real `decimals` field: [5](#0-4) 

But the `Legacy` variant never does this. The deployed `EvmErc20` / `EvmErc20V2` contract stores `_decimals` in its constructor and returns it from `decimals()`: [6](#0-5) [7](#0-6) 

Because `decimals: 0` is encoded into the constructor arguments at deploy time, the on-chain ERC-20 mirror permanently reports `decimals() = 0` to every caller.

---

### Impact Explanation

The `decimals()` return value is the canonical signal used by every EVM-compatible DeFi protocol (AMMs, lending markets, oracles, aggregators) to interpret raw token amounts. When `decimals()` returns `0` for a token that actually has 6 decimals (e.g., USDC):

- 1,000,000 raw units (= 1 real USDC) are treated as **1,000,000 whole tokens**.
- A user deposits 1 USDC worth of ERC-20 units into a lending protocol on Aurora; the protocol reads `decimals() = 0` and prices the collateral at 1,000,000× its true value.
- The user borrows against the inflated collateral, draining the lending pool.
- Conversely, a protocol that prices the token correctly but uses `decimals()` for scaling will compute swap/liquidation amounts that are off by `10^decimals`, causing direct fund loss for counterparties.

This is a direct, reachable fund-theft vector for any user of a DeFi protocol on Aurora that integrates with an ERC-20 mirror deployed via the legacy path. The root cause is entirely within Aurora Engine; no external dependency failure is required.

---

### Likelihood Explanation

The legacy path is the backward-compatible default: any caller that passes a raw Borsh-encoded `AccountId` (the original API) is silently routed to `Legacy`. All ERC-20 mirrors deployed before the `WithMetadata` variant was introduced, and all future deployments by callers unaware of the new variant, will have `decimals: 0`. The `setMetadata` admin function exists but requires a separate privileged transaction; there is no enforcement that it is called after deployment. [8](#0-7) 

---

### Recommendation

1. Change `Erc20Metadata::default()` to use `decimals: 18` as a safer fallback, or — better — remove the `unwrap_or_default()` and require metadata to always be explicitly provided.
2. Deprecate the `Legacy` variant of `DeployErc20TokenArgs` and require `WithMetadata` for all new deployments so that `ft_metadata` is always fetched and the real `decimals` value is used.
3. Emit an on-chain event or revert if `decimals` is `0` and the NEP-141 metadata call was not performed, to prevent silent misconfiguration.

---

### Proof of Concept

1. Call `deploy_erc20_token` with a legacy-encoded `AccountId` (e.g., `usdc.near`) — this is the default for any caller using the old API.
2. `DeployErc20TokenArgs::deserialize` maps this to `Legacy(usdc.near)`.
3. `engine::deploy_erc20_token(usdc.near, None, ...)` is called; `setup_deploy_erc20_input` calls `erc20_metadata.unwrap_or_default()`, yielding `Erc20Metadata { decimals: 0, ... }`.
4. The `EvmErc20` constructor is called with `metadata_decimals = 0`; `_decimals` is stored as `0`.
5. Any EVM caller invoking `decimals()` on the mirror receives `0`.
6. A user bridges 1,000,000 USDC units (= 1 USDC) via `ft_on_transfer`; `receive_erc20_tokens` mints exactly 1,000,000 ERC-20 units — correct at the bridge layer.
7. A lending protocol on Aurora reads `decimals() = 0` and values 1,000,000 units as 1,000,000 whole tokens, allowing the user to borrow against 1,000,000× the true collateral value, draining the protocol. [9](#0-8) [10](#0-9)

### Citations

**File:** engine-types/src/parameters/connector.rs (L316-324)
```rust
impl Default for Erc20Metadata {
    fn default() -> Self {
        Self {
            name: "Empty".to_string(),
            symbol: "EMPTY".to_string(),
            decimals: 0,
        }
    }
}
```

**File:** engine/src/engine.rs (L796-831)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
```

**File:** engine/src/engine.rs (L1305-1313)
```rust
#[must_use]
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
```

**File:** engine/src/engine.rs (L1327-1332)
```rust
    let erc20_metadata = erc20_metadata.unwrap_or_default();

    let deploy_args = ethabi::encode(&[
        ethabi::Token::String(erc20_metadata.name),
        ethabi::Token::String(erc20_metadata.symbol),
        ethabi::Token::Uint(erc20_metadata.decimals.into()),
```

**File:** engine/src/contract_methods/connector.rs (L124-125)
```rust
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;
```

**File:** engine/src/contract_methods/connector.rs (L176-188)
```rust
        let erc20_metadata =
            if let Some(PromiseResult::Successful(bytes)) = handler.promise_result(0) {
                serde_json::from_slice::<FungibleTokenMetadata>(&bytes)
                    .map(|metadata| Erc20Metadata {
                        name: metadata.name,
                        symbol: metadata.symbol,
                        decimals: metadata.decimals,
                    })
                    .map_err(Into::<ParseArgsError>::into)?
            } else {
                return Err(errors::ERR_GETTING_ERC20_FROM_NEP141.into());
            };
        let address = engine::deploy_erc20_token(nep141, Some(erc20_metadata), io, env, handler)?;
```

**File:** engine-types/src/parameters/engine.rs (L357-361)
```rust
impl DeployErc20TokenArgs {
    pub fn deserialize(bytes: &[u8]) -> Result<Self, io::Error> {
        Self::try_from_slice(bytes).or_else(|_| AccountId::try_from_slice(bytes).map(Self::Legacy))
    }
}
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L21-28)
```text
    constructor (string memory metadata_name, string memory metadata_symbol, uint8 metadata_decimals, address admin)
        ERC20(metadata_name, metadata_symbol)
        AdminControlled(admin, 0)
    {
        _name = metadata_name;
        _symbol = metadata_symbol;
        _decimals = metadata_decimals;
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L38-40)
```text
    function decimals() public view override returns (uint8) {
        return _decimals;
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L43-47)
```text
    function setMetadata(string memory metadata_name, string memory metadata_symbol, uint8 metadata_decimals) external onlyAdmin {
        _name = metadata_name;
        _symbol = metadata_symbol;
        _decimals = metadata_decimals;
    }
```
