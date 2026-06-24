### Title
ckERC20 Deposit Helper Contracts Emit Requested `amount` Instead of Actual Received Amount, Enabling Over-Minting for Fee-on-Transfer or Special-Transfer Tokens - (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

Both ckERC20 deposit helper contracts (`CkErc20Deposit.deposit` and `CkDeposit.depositErc20`) call `safeTransferFrom(msg.sender, minterAddress, amount)` and then unconditionally emit the caller-supplied `amount` in the deposit event. The IC ckETH minter scrapes these events and mints ckERC20 tokens equal to the emitted `amount`. For fee-on-transfer ERC20 tokens, or tokens with special `amount == type(uint256).max` behavior (like cUSDCv3), the actual tokens received by the minter address will be less than `amount`, but the minter mints based on the emitted value — breaking the 1:1 peg and over-minting ckERC20.

---

### Finding Description

In `ERC20DepositHelper.sol`, the `CkErc20Deposit.deposit` function:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal); // emits requested amount, not actual received
}
``` [1](#0-0) 

Similarly, in `DepositHelperWithSubaccount.sol`, the `CkDeposit.depositErc20` function:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount); // emits requested amount
}
``` [2](#0-1) 

Neither contract measures the actual balance change of `minterAddress` before and after the `safeTransferFrom` call. The emitted `amount` is the caller-controlled input, not the actual tokens received.

The IC ckETH minter scrapes these `ReceivedErc20` / `ReceivedEthOrErc20` events and uses the `value` field directly to mint ckERC20 tokens on the IC ledger. The `AcceptedErc20Deposit` event in the minter's DID confirms the `value` field from the Ethereum log is used as the mint amount: [3](#0-2) 

Two concrete attack vectors exist:

1. **Fee-on-transfer tokens**: If a supported ckERC20 token charges a transfer fee, `safeTransferFrom(msg.sender, minterAddress, amount)` delivers `amount - fee` to the minter, but the event emits `amount`. The minter mints `amount` ckERC20, over-minting by `fee`.

2. **`type(uint256).max` sentinel value (cUSDCv3-style)**: Tokens like cUSDCv3 treat `amount == type(uint256).max` as "transfer the caller's entire balance." A user with balance `B` calls `depositErc20` with `amount = type(uint256).max`. The `safeTransferFrom` transfers only `B` tokens to the minter, but the event emits `type(uint256).max`. The minter attempts to mint `type(uint256).max` ckERC20 tokens — a catastrophic over-mint.

---

### Impact Explanation

**Ledger conservation break / chain-fusion mint bug.** The 1:1 backing invariant between ERC20 tokens held by the minter's Ethereum address and ckERC20 tokens in circulation on the IC is violated. An attacker can mint unbounded ckERC20 tokens backed by far fewer actual ERC20 tokens. This allows draining the ckERC20 liquidity pool via withdrawal, since the minter will attempt to send ERC20 tokens it does not hold.

---

### Likelihood Explanation

**Medium.** The minter currently enforces a whitelist of supported ERC20 tokens, and standard tokens like USDC/USDT do not currently have fee-on-transfer behavior. However:
- The whitelist is governance-controlled and can be expanded via NNS proposal to include tokens with non-standard transfer semantics.
- The `type(uint256).max` attack vector applies to any token that implements the cUSDCv3 sentinel pattern, which is a known and deployed pattern on Ethereum mainnet.
- Any unprivileged Ethereum user can call `depositErc20` on the helper contract with any ERC20 address — the helper contract has no whitelist. [4](#0-3) 

---

### Recommendation

Both helper contracts should measure the actual balance change of `minterAddress` and emit that value instead of the caller-supplied `amount`:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
    emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
}
```

Apply the same fix to `CkErc20Deposit.deposit` in `ERC20DepositHelper.sol`.

---

### Proof of Concept

1. A cUSDCv3-style ERC20 token `T` is added to the ckERC20 supported list via NNS proposal. Token `T` treats `amount == type(uint256).max` as "transfer caller's full balance."
2. Attacker holds `1000 T` tokens on Ethereum. Attacker approves the helper contract for `type(uint256).max`.
3. Attacker calls `depositErc20(T, type(uint256).max, principal, subaccount)`.
4. `safeTransferFrom` transfers only `1000 T` to the minter (the attacker's actual balance).
5. The helper emits `ReceivedEthOrErc20(T, attacker, type(uint256).max, principal, subaccount)`.
6. The IC minter scrapes the event and mints `type(uint256).max` ckT tokens to the attacker's IC account.
7. Attacker calls `withdraw_erc20` for the full ckT balance, draining all `T` tokens held by the minter across all users. [1](#0-0) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L511-532)
```text
    function depositErc20(
        address erc20Address,
        uint256 amount,
        bytes32 principal,
        bytes32 subaccount
    ) public {
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
        IERC20 erc20Token = IERC20(erc20Address);
        erc20Token.safeTransferFrom(
            msg.sender,
            minterAddress,
            amount
        );

        emit ReceivedEthOrErc20(
            erc20Address,
            msg.sender,
            amount,
            principal,
            subaccount
        );
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L557-566)
```text
        AcceptedErc20Deposit : record {
            transaction_hash : text;
            block_number : nat;
            log_index : nat;
            from_address : text;
            value : nat;
            "principal" : principal;
            erc20_contract_address : text;
            subaccount : opt Subaccount;
        };
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L182-191)
```text
[WARNING]
.Supported ERC-20 tokens
====
Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it. To avoid any loss of funds, please verify **before** any important transfer that the desired ERC-20 token is supported by querying the minter as follows
and checking the field `supported_ckerc20_tokens`:
[source,shell]
----
dfx canister --network ic call minter get_minter_info
----
====
```
