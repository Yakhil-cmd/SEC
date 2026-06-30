### Title
Immutable `admin` Variable in `AdminControlled` with No Setter Permanently Blocks `mint` on `EvmErc20`/`EvmErc20V2` - (File: `etc/eth-contracts/contracts/AdminControlled.sol`)

---

### Summary

`AdminControlled.sol` sets the `admin` address once in its constructor and provides no function to update it. Both `EvmErc20` and `EvmErc20V2` inherit this contract and gate the critical `mint` function behind the `onlyAdmin` modifier. If the admin address ever becomes unreachable — for example, after an Aurora Engine contract migration to a new account address — `mint` is permanently inaccessible, blocking all future bridge deposits and permanently freezing in-motion user funds.

---

### Finding Description

`AdminControlled.sol` declares `admin` as a plain storage variable and assigns it exactly once in the constructor: [1](#0-0) 

There is no `setAdmin()` or equivalent function anywhere in the contract or its inheritors. The `onlyAdmin` modifier enforces that only this fixed address may call privileged functions: [2](#0-1) 

`EvmErc20` and `EvmErc20V2` both inherit `AdminControlled` and use `onlyAdmin` to gate `mint`: [3](#0-2) [4](#0-3) 

`mint` is the sole mechanism by which the Aurora Engine credits bridged ERC-20 tokens to users on the EVM side. The `admin` passed at deployment is the Aurora Engine contract account. If the engine is ever migrated to a new NEAR account address (a routine upgrade path), the deployed `EvmErc20`/`EvmErc20V2` contracts retain the old engine address as `admin` with no on-chain way to update it.

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

Once the admin address is stale or unreachable, `mint` reverts for every caller: [2](#0-1) 

Every subsequent bridge deposit that requires minting ERC-20 tokens on the Aurora side will fail permanently. Users who have already initiated a cross-chain transfer (funds locked on the NEAR/Ethereum side, mint not yet executed) will have their assets frozen with no recovery path, because there is also no `setAdmin` to hand control to the new engine address.

Additionally, `adminPause` — the emergency circuit-breaker — is also gated by `onlyAdmin`: [5](#0-4) 

This means the contract cannot be paused in an emergency either.

---

### Likelihood Explanation

Aurora Engine upgrades that change the engine's NEAR account address are a realistic operational event. The `EvmErc20`/`EvmErc20V2` contracts are deployed per bridged token and each stores the engine address as `admin` at construction time. Any engine migration without a prior `setAdmin` call (which is impossible given the missing function) leaves every deployed token contract permanently orphaned. The slither suppression comment on line 6 (`// slither-disable-next-line immutable-states`) confirms the team is aware of the static-analysis flag but chose not to add a setter. [6](#0-5) 

---

### Recommendation

Add a `setAdmin` function to `AdminControlled.sol` protected by `onlyAdmin`, allowing the current admin to transfer the role to a new address before a migration:

```solidity
function setAdmin(address newAdmin) external onlyAdmin {
    require(newAdmin != address(0), "zero address");
    admin = newAdmin;
}
```

This mirrors the fix recommended in the external report (adding a setter for the privileged role variable) and is the standard two-step ownership-transfer pattern used in OpenZeppelin's `Ownable`.

---

### Proof of Concept

1. Deploy `EvmErc20` with `admin = address(AuroraEngineV1)`.
2. Aurora Engine is upgraded; the new engine lives at `address(AuroraEngineV2)`.
3. A user initiates a NEAR→Aurora bridge deposit. The engine calls `EvmErc20.mint(user, amount)` from `AuroraEngineV2`.
4. `onlyAdmin` checks `msg.sender == admin` → `AuroraEngineV2 != AuroraEngineV1` → `require` reverts.
5. The mint never executes. The user's funds are locked on the NEAR side with no corresponding ERC-20 credit on Aurora. No `setAdmin` exists to fix this. The token contract is permanently bricked for all future deposits. [7](#0-6) [8](#0-7)

### Citations

**File:** etc/eth-contracts/contracts/AdminControlled.sol (L6-16)
```text
    // slither-disable-next-line immutable-states
    address public admin;
    uint public paused;

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

**File:** etc/eth-contracts/contracts/AdminControlled.sol (L28-30)
```text
    function adminPause(uint flags) public onlyAdmin {
        paused = flags;
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L15-28)
```text
contract EvmErc20 is ERC20, AdminControlled, IExit {
    string private _name;
    string private _symbol;
    uint8 private _decimals;

    // slither-disable-next-line shadowing-local
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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```
