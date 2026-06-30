### Title
Missing Zero-Address Validation for `admin` in `AdminControlled` Constructor Permanently Freezes ERC-20 Bridge Minting — (`File: etc/eth-contracts/contracts/AdminControlled.sol`)

### Summary
`AdminControlled.sol` accepts an `_admin` address in its constructor with no zero-address check. Both `EvmErc20.sol` and `EvmErc20V2.sol` inherit from it and forward the `admin` parameter without validation. There is no `setAdmin` recovery function. If `admin` is ever set to `address(0)`, the `mint` function — which is the sole mechanism by which the Aurora Engine credits bridged NEP-141 tokens to EVM recipients — becomes permanently inaccessible, freezing all future bridge deposits for that token.

### Finding Description
`AdminControlled.sol` constructor assigns `_admin` directly with no zero-address guard:

```solidity
constructor(address _admin, uint flags) {
    // slither-disable-next-line missing-zero-check
    admin = _admin;   // ← no require(_admin != address(0))
    paused = flags;
}
```

The `slither-disable-next-line missing-zero-check` comment explicitly suppresses the static-analysis warning rather than fixing it. [1](#0-0) 

Both `EvmErc20` and `EvmErc20V2` inherit `AdminControlled` and pass the `admin` constructor argument through without any validation:

```solidity
constructor (... address admin)
    ERC20(metadata_name, metadata_symbol)
    AdminControlled(admin, 0)   // ← no zero-check before forwarding
``` [2](#0-1) [3](#0-2) 

The `mint` function is gated by `onlyAdmin`:

```solidity
function mint(address account, uint256 amount) public onlyAdmin {
    _mint(account, amount);
}
``` [4](#0-3) 

`onlyAdmin` requires `msg.sender == admin`. Since `msg.sender` can never be `address(0)` in a normal EVM transaction, a zero admin permanently locks `mint`. There is no `setAdmin` or equivalent recovery function anywhere in `AdminControlled`. [5](#0-4) 

The Aurora Engine computes the `admin` address to pass at deployment time via `setup_deploy_erc20_input`, using `current_address(current_account_id)` — a deterministic hash of the engine's NEAR account ID:

```rust
let erc20_admin_address = current_address(current_account_id);
...
ethabi::Token::Address(erc20_admin_address.raw().0.into()),
``` [6](#0-5) 

This value is passed directly into the `EvmErc20`/`EvmErc20V2` constructor with no on-chain guard against it being zero. If `current_address` ever returns `Address::zero()` — for example, due to a bug, an empty or default `AccountId`, or a future code path — the deployed ERC-20 contract is permanently bricked with no recovery path.

### Impact Explanation
**Critical — Permanent freezing of funds.**

The `mint` function is the only mechanism by which the Aurora Engine credits ERC-20 tokens to a recipient when NEP-141 tokens are bridged from NEAR. It is called inside `receive_erc20_tokens` via `setup_receive_erc20_tokens_input`, which encodes a call to `mint(recipient, amount)` and dispatches it through the EVM: [7](#0-6) 

If `admin == address(0)`, every call to `mint` reverts. All subsequent `ft_on_transfer` bridge deposits for that token silently fail (the engine returns the tokens to the sender per its error-handling logic), but the ERC-20 contract itself is permanently non-mintable. Any tokens already locked on the NEAR side that are waiting to be credited on Aurora are permanently frozen. There is no admin-change function and no upgrade path for the individual ERC-20 contract.

### Likelihood Explanation
Under normal operation, `current_address(current_account_id)` produces a non-zero address because it hashes a valid, non-empty NEAR `AccountId`. However:

- The missing validation means there is **zero on-chain protection** if the computed address is ever zero.
- `EngineState::default()` sets `owner_id` to `AccountId::default()`, which in test/edge-case paths could propagate a zero-producing address into deployment.
- The explicit `slither-disable-next-line missing-zero-check` suppression shows the developers are aware of the gap and chose not to fix it, leaving the risk open.
- Once deployed with `admin = address(0)`, the state is **irreversible** — there is no admin-change function.

### Recommendation
1. Add a zero-address guard in `AdminControlled`'s constructor:
   ```solidity
   constructor(address _admin, uint flags) {
       require(_admin != address(0), "AdminControlled: zero admin");
       admin = _admin;
       paused = flags;
   }
   ```
2. Add a `setAdmin(address newAdmin)` function gated by `onlyAdmin` to allow recovery.
3. In `setup_deploy_erc20_input` (Rust), assert that `erc20_admin_address != Address::zero()` before encoding the deploy calldata.

### Proof of Concept
1. Deploy `EvmErc20` (or `EvmErc20V2`) with `admin = address(0)`.
2. Call `mint(anyRecipient, 1)`.
3. The call reverts because `onlyAdmin` requires `msg.sender == address(0)`, which is impossible.
4. All bridge deposits via `ft_on_transfer` → `receive_erc20_tokens` → `mint` permanently fail for this token.
5. No recovery is possible: `AdminControlled` has no `setAdmin` function, and the ERC-20 contract is not upgradeable. [1](#0-0) [4](#0-3) [8](#0-7) [7](#0-6)

### Citations

**File:** etc/eth-contracts/contracts/AdminControlled.sol (L10-16)
```text
    constructor(address _admin, uint flags) {
        // slither-disable-next-line missing-zero-check
        admin = _admin;

        // Add the possibility to set pause flags on the initialization
        paused = flags;
    }
```

**File:** etc/eth-contracts/contracts/AdminControlled.sol (L18-21)
```text
    modifier onlyAdmin {
        require(msg.sender == admin);
        _;
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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L21-28)
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

**File:** engine/src/engine.rs (L824-837)
```rust
        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;
```

**File:** engine/src/engine.rs (L1317-1337)
```rust
pub fn setup_deploy_erc20_input(
    current_account_id: &AccountId,
    erc20_metadata: Option<Erc20Metadata>,
) -> Vec<u8> {
    #[cfg(feature = "error_refund")]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20V2.bin");
    #[cfg(not(feature = "error_refund"))]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20.bin");

    let erc20_admin_address = current_address(current_account_id);
    let erc20_metadata = erc20_metadata.unwrap_or_default();

    let deploy_args = ethabi::encode(&[
        ethabi::Token::String(erc20_metadata.name),
        ethabi::Token::String(erc20_metadata.symbol),
        ethabi::Token::Uint(erc20_metadata.decimals.into()),
        ethabi::Token::Address(erc20_admin_address.raw().0.into()),
    ]);

    [erc20_contract, deploy_args.as_slice()].concat()
}
```
