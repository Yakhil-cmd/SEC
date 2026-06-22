### Title
ckERC20 Minter Mints Based on Requested Transfer Amount, Not Actual Received Amount, Breaking 1:1 Backing for Deflationary ERC-20 Tokens - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

Both ckERC20 deposit helper contracts emit the `ReceivedEthOrErc20` / `ReceivedErc20` event using the caller-supplied `amount` parameter — not the actual tokens received by the minter's Ethereum address. The IC minter canister reads this event and mints exactly `event.value()` ckERC20 tokens to the user. For any deflationary ERC-20 token (one that deducts a fee on `transferFrom`), the minter's Ethereum address receives strictly less than `amount`, yet the minter mints the full `amount` of ckERC20 and records the full `amount` in its internal ERC-20 balance. This breaks the 1:1 backing invariant of the ckERC20 system.

---

### Finding Description

**Root cause — helper contracts (Ethereum side):**

`DepositHelperWithSubaccount.sol::depositErc20` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then unconditionally emits the event with the caller-supplied `amount`:

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
``` [1](#0-0) 

The legacy `ERC20DepositHelper.sol::deposit` has the identical pattern:

```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
``` [2](#0-1) 

For a deflationary ERC-20 token, `safeTransferFrom` succeeds (it does not revert), but the minter's address receives `amount - fee` tokens. The event, however, records `amount`. There is no balance-before/balance-after check.

**Root cause — IC minter canister (IC side):**

The `mint()` function in `deposit.rs` reads `event.value()` directly from the scraped log event and passes it as the mint amount to the ICRC-1 ledger:

```rust
amount: event.value(),
``` [3](#0-2) 

The minter also updates its internal ERC-20 balance accounting using the same unverified event value:

```rust
ReceivedEvent::Erc20(event) => self
    .erc20_balances
    .erc20_add(event.erc20_contract_address, event.value),
``` [4](#0-3) 

Neither the helper contracts nor the minter canister verify that the actual tokens received equal the event-recorded amount.

---

### Impact Explanation

**Ledger conservation bug / chain-fusion mint/burn/replay bug.**

For every deposit of a deflationary ERC-20 token, the minter mints `amount` ckERC20 but holds only `amount - fee` ERC-20 tokens. The ckERC20 supply exceeds the ERC-20 collateral held by the minter. When users later withdraw ckERC20 back to ERC-20, the minter will eventually be unable to fulfill all withdrawal requests — the last withdrawers will find the minter's Ethereum address has insufficient ERC-20 balance. The minter's internal `erc20_balances` accounting is also inflated, masking the shortfall from any invariant checks.

---

### Likelihood Explanation

The minter enforces a whitelist of supported ERC-20 tokens via NNS governance. Currently supported tokens (USDC, USDT, etc.) are not deflationary. However:

1. Several widely-used ERC-20 tokens are upgradeable proxies. If the token owner adds a transfer fee after the token is whitelisted, the minter immediately begins over-minting with no code change required on the IC side.
2. A future NNS proposal could add a deflationary token (e.g., a token with a built-in burn-on-transfer mechanism) as a supported ckERC20 token. The minter code contains no guard against this.

The attacker entry path requires only that a supported ERC-20 token charges a fee on `transferFrom` — an unprivileged user then simply calls `depositErc20` with any amount to trigger the over-minting.

---

### Recommendation

In both helper contracts, measure the minter's actual balance change and emit that as the event value:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

Alternatively, restrict the helper contracts to a whitelist of non-deflationary tokens, or add an explicit check that `actualReceived == amount` and revert otherwise.

---

### Proof of Concept

1. A supported ERC-20 token (or a newly whitelisted one) charges a 1% fee on every `transferFrom`.
2. User calls `depositErc20(tokenAddress, 1_000_000, principal, subaccount)` on `DepositHelperWithSubaccount.sol`.
3. `safeTransferFrom` succeeds; minter's Ethereum address receives `990_000` tokens.
4. The helper emits `ReceivedEthOrErc20(..., amount=1_000_000, ...)`.
5. The IC minter scrapes the log, reads `value = 1_000_000`, and calls `icrc1_transfer` on the ckERC20 ledger to mint `1_000_000` ckERC20 to the user.
6. The minter's `erc20_balances` is incremented by `1_000_000` despite holding only `990_000`.
7. Repeating this inflates the unbacked ckERC20 supply. When total withdrawals exceed the minter's actual ERC-20 holdings, withdrawal transactions will fail or drain other users' collateral.

### Citations

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L519-531)
```text
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
```

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-81)
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L335-338)
```rust
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
```
