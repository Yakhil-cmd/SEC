### Title
ckERC20 Deposit Helper Emits Requested Amount Instead of Actual Received Amount, Enabling ckERC20 Undercollateralization with Fee-on-Transfer ERC20 Tokens — (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckERC20 deposit helper contracts (`DepositHelperWithSubaccount.sol` and `ERC20DepositHelper.sol`) emit the caller-supplied `amount` parameter in the `ReceivedEthOrErc20` / `ReceivedErc20` event rather than the actual ERC20 balance change received by the minter's Ethereum address. The IC ckETH minter canister reads this event and mints exactly `event.value()` ckERC20 tokens without independently verifying the actual received amount. If any supported ERC20 token implements fee-on-transfer behavior — either by design or via an upgrade — the minter will mint more ckERC20 than the ERC20 it actually holds, permanently undercollateralizing the ckERC20 token.

---

### Finding Description

**Root cause in the Solidity helper contracts:**

`DepositHelperWithSubaccount.sol` (`depositErc20`):

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);  // actual received may be < amount

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        amount,          // ← emits the REQUESTED amount, not the actual received amount
        principal,
        subaccount
    );
}
``` [1](#0-0) 

The same pattern exists in `ERC20DepositHelper.sol`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);  // ← requested, not received
}
``` [2](#0-1) 

Neither contract checks the minter's ERC20 balance before and after `safeTransferFrom` to determine the actual received amount. For a fee-on-transfer token, `safeTransferFrom(user, minter, 1000)` may result in the minter receiving only `950`, but the event records `1000`.

**Root cause in the IC minter canister:**

The `mint()` function in `rs/ethereum/cketh/minter/src/deposit.rs` reads the Ethereum event and mints `event.value()` ckERC20 tokens directly:

```rust
let block_index = match client
    .transfer(TransferArg {
        to: event.beneficiary(),
        amount: event.value(),   // ← trusts the event amount without verification
        ...
    })
    .await
``` [3](#0-2) 

`event.value()` is the `amount` field from the Ethereum log, which is the requested transfer amount, not the actual received amount. The minter has no mechanism to independently verify the actual ERC20 balance change at the minter's Ethereum address.

The deposit flow documented in `rs/ethereum/cketh/docs/ckerc20.adoc` confirms the minter mints `amount` directly from the event:

```
ReceivedEthOrErc20(token_id, user, amount, principal, subaccount)
────────────────────────────────────────────────────────────────>
mint(token_id, amount, principal, subaccount)
``` [4](#0-3) 

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn/replay bug — incorrect internal balance bookkeeping in the ckERC20 minting flow.

If a supported ERC20 token implements fee-on-transfer behavior:

1. A user deposits `N` tokens via `depositErc20`. The ERC20 contract deducts a fee, so the minter's Ethereum address receives only `N - fee`.
2. The helper contract emits `amount = N` in the event.
3. The IC minter mints `N` ckERC20 tokens to the user.
4. The minter's Ethereum address holds only `N - fee` ERC20 tokens.
5. The ckERC20 token is now undercollateralized by `fee` per deposit.
6. Repeated deposits compound the undercollateralization.
7. When users attempt to withdraw ckERC20 back to ERC20, the minter will eventually be unable to fulfill all withdrawal requests, causing a loss of funds for some ckERC20 holders.

This is a direct ledger conservation violation: the total ckERC20 supply exceeds the ERC20 held by the minter.

---

### Likelihood Explanation

The documentation explicitly states that the helper contract does not enforce a whitelist of ERC20 tokens — this is enforced only by the minter via NNS governance:

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens." [5](#0-4) 

Two realistic trigger paths exist without requiring a malicious governance majority:

1. **Token upgrade:** A currently supported ERC20 token (e.g., USDT, which has historically had fee-on-transfer functionality in its contract) is upgraded by its issuer to enable fee-on-transfer. No NNS action is required; any user depositing the upgraded token triggers the bug.
2. **New token addition:** A new ERC20 token with fee-on-transfer is added as a supported ckERC20 token via NNS proposal. The NNS proposal process does not currently enforce a technical check against fee-on-transfer behavior.

The first path requires no privileged IC action and is triggered by any unprivileged user depositing the affected token.

---

### Recommendation

**Fix in the Solidity helper contracts:** Measure the actual received amount by checking the minter's ERC20 balance before and after `safeTransferFrom`, and emit the delta:

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        actualReceived,   // ← emit actual received, not requested amount
        principal,
        subaccount
    );
}
```

**Fix in the NNS governance process:** Add a technical requirement that ERC20 tokens added as supported ckERC20 tokens must not implement fee-on-transfer behavior, and document this as an invariant that the minter relies upon.

---

### Proof of Concept

1. Suppose USDT (a currently supported ckERC20 token) re-enables its fee-on-transfer mechanism (historically present in its contract) at a rate of 5%.
2. Alice calls `depositErc20(USDT_ADDRESS, 1000e6, alice_principal, subaccount)` on the helper contract.
3. USDT's `transferFrom` deducts 5%, so the minter's Ethereum address receives `950e6` USDT.
4. The helper contract emits `ReceivedEthOrErc20(USDT_ADDRESS, alice, 1000e6, alice_principal, subaccount)`.
5. The IC minter reads the event and calls `mint(alice_account, 1000e6)` on the ckUSDT ledger.
6. Alice holds `1000e6` ckUSDT but the minter only holds `950e6` USDT.
7. After 20 such deposits, the minter holds `19000e6` USDT but has issued `20000e6` ckUSDT — a `1000e6` USDT shortfall.
8. The last user to attempt withdrawal of ckUSDT will find the minter unable to fulfill the request, resulting in permanent loss of funds.

The minting code path that trusts `event.value()` without verification: [3](#0-2) 

The helper contract that emits the requested amount instead of the actual received amount: [6](#0-5)

### Citations

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-82)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
            .await
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L130-133)
```text
   │                                 │                                                      │ReceivedEthOrErc20(token_id, user, amount, principal, subaccount)│
   │                                 │                                                      │────────────────────────────────────────────────────────────────>│
   │                                 │     mint(token_id, amount, principal, subaccount)    │                                                                 │
   │<─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────│
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
