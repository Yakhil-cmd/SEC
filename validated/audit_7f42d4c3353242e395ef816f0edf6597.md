### Title
ckERC20 Minter Mints Based on Event-Logged Amount, Not Actual Received Amount, Enabling Undercollateralization via Fee-on-Transfer Tokens - (File: rs/ethereum/cketh/minter/ERC20DepositHelper.sol, rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

### Summary

Both ckERC20 deposit helper contracts emit deposit events using the caller-supplied `amount` parameter rather than the actual amount received by the minter address. The IC ckETH minter canister trusts this event value and mints exactly that many ckERC20 tokens. For fee-on-transfer ERC20 tokens (such as USDT, which is an explicitly supported ckERC20 token and has a built-in fee mechanism), the minter address receives `amount - fee` but mints `amount` ckERC20 tokens, permanently undercollateralizing the ckERC20 supply.

### Finding Description

The `CkErc20Deposit.deposit()` function in `ERC20DepositHelper.sol` and the `CkDeposit.depositErc20()` function in `DepositHelperWithSubaccount.sol` both follow the same pattern:

1. Call `safeTransferFrom(msg.sender, minterAddress, amount)` to pull tokens from the user.
2. Emit a deposit event with the caller-supplied `amount` as the deposit value.

Neither contract checks the actual balance change at `minterAddress` before emitting the event.

In `ERC20DepositHelper.sol`:
```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal); // amount, not actual received
}
``` [1](#0-0) 

In `DepositHelperWithSubaccount.sol`:
```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount); // amount, not actual received
``` [2](#0-1) 

The IC ckETH minter canister scrapes these Ethereum logs via `ReceivedErc20LogParser` and `ReceivedEthOrErc20LogParser`, parsing the `amount` field from the event data directly into `ReceivedErc20Event.value`: [3](#0-2) 

The `mint()` function in `deposit.rs` then calls the ICRC-1 ledger to mint exactly `event.value()` ckERC20 tokens to the beneficiary — with no cross-check against the actual ERC20 balance held by the minter: [4](#0-3) 

The `value()` method on `ReceivedEvent` returns the value parsed from the event log, which is the caller-supplied `amount`, not the actual received amount: [5](#0-4) 

### Impact Explanation

**Vulnerability class: chain-fusion mint/burn/replay bug (ledger conservation bug)**

For every deposit of a fee-on-transfer ERC20 token, the ckERC20 ledger mints more tokens than the actual ERC20 backing held by the minter. The discrepancy accumulates with each deposit. Eventually, the last users to withdraw cannot redeem their full ckERC20 balance because the minter holds insufficient ERC20 tokens to cover all outstanding ckERC20. The ckERC20 token becomes permanently undercollateralized.

USDT (`0xdAC17F958D2ee523a2206206994597C13D831ec7`) is an explicitly supported ckERC20 token (ckUSDT): [6](#0-5) [7](#0-6) 

USDT's contract contains a fee mechanism (`basisPointsRate`, `maximumFee`) that is currently set to zero but can be enabled at any time by the Tether contract owner. If enabled, every ckUSDT deposit would mint more ckUSDT than the USDT received, breaking the 1:1 backing guarantee.

### Likelihood Explanation

The likelihood is **medium**. The vulnerability requires a supported ERC20 token to activate its fee mechanism. USDT — a live, supported ckERC20 token — has this capability built into its contract. The Tether organization has historically used and modified contract parameters. No attacker capability beyond calling the public `depositErc20` function is required once fees are active. Any ordinary user depositing USDT after fee activation would trigger the undercollateralization, even without malicious intent.

### Recommendation

**Short-term:** In both helper contracts, measure the actual balance change at `minterAddress` before and after the `safeTransferFrom` call, and emit the event with the actual received amount:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

**Long-term:** The minter canister should additionally validate that the ERC20 balance of the minter address increased by at least the event-logged amount before minting ckERC20 tokens. Consider adding a policy to reject fee-on-transfer tokens from the supported token list.

### Proof of Concept

1. USDT contract owner enables a 0.1% transfer fee (`basisPointsRate = 10`).
2. User calls `depositErc20(USDT_ADDRESS, 1_000_000, principal, subaccount)` on the `CkDeposit` helper contract (`DepositHelperWithSubaccount.sol`).
3. `safeTransferFrom` executes: minter receives `999_000` USDT (after 0.1% fee of `1_000`).
4. Helper emits `ReceivedEthOrErc20(USDT, user, 1_000_000, principal, subaccount)` — with the full `1_000_000`.
5. IC ckETH minter scrapes the log, parses `value = 1_000_000` from the event data. [8](#0-7) 
6. Minter calls `icrc1_transfer` on the ckUSDT ledger with `amount = 1_000_000`, minting `1_000_000` ckUSDT to the user. [4](#0-3) 
7. After N such deposits, the minter holds `N × 999_000` USDT but has minted `N × 1_000_000` ckUSDT. The `1_000 × N` shortfall means the last depositors cannot withdraw their full balance, breaking the 1:1 peg.

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L86-102)
```rust
        let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value: Erc20Value::from_be_bytes(value_bytes),
            principal,
            erc20_contract_address,
            subaccount: None,
        }
        .into())
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L127-160)
```rust
        let [value_bytes, subaccount_bytes] =
            parse_hex_into_32_byte_words(entry.data, event_source)?;
        let subaccount = LedgerSubaccount::from_bytes(subaccount_bytes);
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        if erc20_contract_address == Address::ZERO {
            let value = Wei::from_be_bytes(value_bytes);
            return Ok(ReceivedEthEvent {
                transaction_hash,
                block_number,
                log_index,
                from_address,
                value,
                principal,
                subaccount,
            }
            .into());
        }

        let value = Erc20Value::from_be_bytes(value_bytes);
        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value,
            principal,
            erc20_contract_address,
            subaccount,
        }
        .into())
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L205-210)
```rust
    pub fn value(&self) -> candid::Nat {
        match self {
            ReceivedEvent::Eth(evt) => evt.value.into(),
            ReceivedEvent::Erc20(evt) => evt.value.into(),
        }
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L37-38)
```text
|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]
```

**File:** rs/ethereum/cketh/mainnet/orchestrator_upgrade_2024_07_22_ckusdt.md (L1-15)
```markdown
# Proposal to upgrade the ledger suite orchestrator canister to add ckUSDT

Git hash: `de29a1a55b589428d173b31cdb8cec0923245657`

New compressed Wasm hash: `81f426bcc52140fdcf045d02d00b04bfb4965445b8aed7090d174fcdebf8beea`

Target canister: `vxkom-oyaaa-aaaar-qafda-cai`

Previous ledger suite orchestrator proposal: https://dashboard.internetcomputer.org/proposal/131373

---

## Motivation

This proposal upgrades the ckERC20 ledger suite orchestrator to add support for [USDT](https://etherscan.io/token/0xdac17f958d2ee523a2206206994597c13d831ec7#tokenInfo). Once executed, the twin token ckUSDT will be available on ICP, refer to the [documentation](https://github.com/dfinity/ic/blob/master/rs/ethereum/cketh/docs/ckerc20.adoc) on how to proceed with deposits and withdrawals.
```
