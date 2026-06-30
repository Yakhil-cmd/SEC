### Title
Missing Zero Address Check for `admin` in `AdminControlled` Constructor Permanently Locks ERC-20 Mint and Admin Functions - (File: `etc/eth-contracts/contracts/AdminControlled.sol`)

---

### Summary

`AdminControlled.sol` sets `admin` in its constructor without validating against `address(0)`. The contract contains no `setAdmin` or `transferAdmin` function, making the admin address immutable after deployment. `EvmErc20` and `EvmErc20V2` inherit from `AdminControlled` and gate their `mint` function behind `onlyAdmin`. If `admin` is set to `address(0)` at deployment, minting is permanently disabled, permanently freezing the ability to bridge NEP-141 tokens into Aurora as ERC-20s.

---

### Finding Description

`AdminControlled.sol` constructor:

```solidity
constructor(address _admin, uint flags) {
    // slither-disable-next-line missing-zero-check
    admin = _admin;
    paused = flags;
}
```

The `// slither-disable-next-line missing-zero-check` comment explicitly acknowledges the missing validation. The contract exposes no mechanism to update `admin` after construction — the only functions defined are `adminPause`, `adminSstore`, `adminSendEth`, `adminReceiveEth`, and `adminDelegatecall`, all of which are themselves gated by `onlyAdmin`:

```solidity
modifier onlyAdmin {
    require(msg.sender == admin);
    _;
}
```

`EvmErc20` and `EvmErc20V2` both inherit `AdminControlled` and pass the `admin` constructor argument directly:

```solidity
constructor (string memory metadata_name, string memory metadata_symbol, uint8 metadata_decimals, address admin)
    ERC20(metadata_name, metadata_symbol)
    AdminControlled(admin, 0)
```

Both contracts expose a `mint` function gated by `onlyAdmin`:

```solidity
function mint(address account, uint256 amount) public onlyAdmin {
    _mint(account, amount);
}
```

`mint` is the mechanism by which the Aurora Engine credits bridged NEP-141 tokens to EVM recipients. It is called via `setup_receive_erc20_tokens_input` in `engine/src/engine.rs`. If `admin == address(0)`, `mint` is permanently inaccessible because no EVM transaction can originate from `address(0)`.

---

### Impact Explanation

If `admin` is `address(0)` at deployment:

- `mint` is permanently locked → no NEP-141 tokens can ever be bridged into Aurora for that ERC-20 → any NEAR-side deposits targeting that token are permanently frozen on the NEAR side.
- `adminPause`, `adminSstore`, `adminSendEth`, `adminDelegatecall` are all permanently locked with no recovery path.
- There is no `setAdmin` function anywhere in `AdminControlled`, `EvmErc20`, or `EvmErc20V2`.

**Impact: High — Permanent freezing of funds** (bridged token deposits become permanently unclaimable on Aurora).

---

### Likelihood Explanation

The Aurora Engine's own `deploy_erc20_token` path in `engine/src/engine.rs` derives admin as `current_address(current_account_id)`, which is a keccak-based derivation of the NEAR account ID and will not be `address(0)` in practice. However:

- `EvmErc20` and `EvmErc20V2` are standalone Solidity contracts with public constructors accepting `admin` as a caller-supplied parameter.
- A contract deployer interacting directly with the EVM (bypassing the Aurora Engine's Rust deployment path) can supply `address(0)` as `admin`.
- The `// slither-disable-next-line missing-zero-check` suppression confirms the absence of any on-chain guard.
- There is zero recovery path once deployed with zero admin.

**Likelihood: Low** — requires a deployer to supply `address(0)`, but the contract provides no on-chain defense against it.

---

### Recommendation

Add a zero address check in `AdminControlled`'s constructor:

```solidity
constructor(address _admin, uint flags) {
    require(_admin != address(0), "AdminControlled: admin is zero address");
    admin = _admin;
    paused = flags;
}
```

Additionally, consider adding a `setAdmin` function (with `onlyAdmin` guard) to allow recovery from misconfiguration, mirroring the pattern of the external report's recommendation.

---

### Proof of Concept

1. Deploy `EvmErc20` (or `EvmErc20V2`) directly on Aurora EVM with `admin = address(0)`:
   ```solidity
   EvmErc20 token = new EvmErc20("Token", "TKN", 18, address(0));
   ```
2. Attempt to call `mint`:
   ```solidity
   token.mint(someUser, 1e18); // reverts: require(msg.sender == admin) → require(msg.sender == address(0))
   ```
3. No EVM transaction can satisfy `msg.sender == address(0)`, so `mint` is permanently inaccessible.
4. Any NEP-141 deposits routed to this ERC-20 contract on NEAR will never be credited on Aurora — funds are permanently frozen.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

**File:** engine/src/engine.rs (L1306-1314)
```rust
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
}
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
