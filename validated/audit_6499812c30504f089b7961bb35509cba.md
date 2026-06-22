### Title
ckETH Minter Mints ckERC20 Based on Event-Reported Amount Without Verifying Actual Received Balance, Enabling Over-Minting for Fee-on-Transfer Tokens - (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter's deposit flow mints ckERC20 tokens using the `amount` field from the Ethereum log event, which reflects the *requested* transfer amount, not the *actual* amount received by the minter's Ethereum address. For fee-on-transfer ERC20 tokens, these values differ. Because the minter performs no before/after balance check to verify actual receipt, it can mint more ckERC20 than the ERC20 tokens it actually holds, breaking the 1:1 peg and causing loss of funds for later withdrawers.

---

### Finding Description

**Deposit flow (Ethereum side):**

The helper contract `CkDeposit.depositErc20()` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits `ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount)`. The `amount` in the event is the *parameter passed by the caller*, not the actual amount received by `minterAddress` after any transfer fee deduction. [1](#0-0) 

**Deposit flow (IC minter side):**

The `mint()` function in `rs/ethereum/cketh/minter/src/deposit.rs` reads `event.value()` directly from the scraped Ethereum log and passes it as the `amount` to `icrc1_transfer` (the minting call to the ckERC20 ledger). There is no balance check before or after the Ethereum transfer to verify that the minter's address actually received the full `amount`. [2](#0-1) 

For a fee-on-transfer ERC20 token with fee rate `f`:
- Minter's Ethereum address receives: `amount * (1 - f)`
- ckERC20 minted on IC: `amount`
- Over-minting per deposit: `amount * f`

**USDT is explicitly listed as a supported ckERC20 token** and USDT has a fee mechanism that is currently disabled but can be enabled by the Tether contract owner. XAUt (Tether Gold) is also listed as supported and has fee mechanisms. [3](#0-2) 

The minter's internal ERC20 balance tracking (`erc20_balances`) is updated via `update_balance_upon_deposit` using the event-reported value, not the actual on-chain balance, compounding the accounting error. [4](#0-3) 

---

### Impact Explanation

**Impact: Medium**

- The ckERC20 token loses its 1:1 backing with the underlying ERC20 token.
- The minter's Ethereum address holds fewer ERC20 tokens than the total ckERC20 supply implies.
- Later withdrawers who attempt to redeem ckERC20 → ERC20 will find insufficient ERC20 balance at the minter's address, resulting in failed withdrawals and permanent loss of funds for some users.
- The minter's internal `erc20_balances` accounting diverges from the actual on-chain balance, making the discrepancy invisible to the minter's own monitoring.

---

### Likelihood Explanation

**Likelihood: Medium**

- USDT (explicitly supported as ckUSDT) has a dormant fee mechanism controlled by Tether. If Tether enables it, every USDT deposit would over-mint ckUSDT.
- XAUt (Tether Gold, also supported) has similar fee mechanisms.
- The minter has no guard against fee-on-transfer behavior for any currently or future supported ERC20 token.
- No attacker action is required on the IC side; the vulnerability is triggered by normal user deposits once a supported token's fee is active.

---

### Recommendation

1. **Add before/after balance checks**: Before and after calling `safeTransferFrom`, record the minter's ERC20 balance on Ethereum. Use the difference (actual received amount) as the mint amount, not the event-reported `amount`.

2. **Alternatively**, explicitly document and enforce that fee-on-transfer tokens are not supported, and add an on-chain validation step that rejects deposits where the event amount does not match the actual balance delta (verifiable via `eth_call` to `balanceOf`).

3. **Audit supported token list**: USDT and XAUt have dormant fee mechanisms. Either remove them from the supported list or implement the balance-check mitigation before re-enabling support.

---

### Proof of Concept

1. USDT contract owner enables the USDT transfer fee (e.g., 1 basis point = 0.01%).
2. User calls `depositErc20(USDT_ADDRESS, 1_000_000_000, principal, subaccount)` on the helper contract.
3. Helper contract calls `USDT.safeTransferFrom(user, minter, 1_000_000_000)`.
4. Due to the 0.01% fee, minter receives `999_900_000` USDT; helper emits event with `amount = 1_000_000_000`.
5. IC minter scrapes the log, reads `event.value() = 1_000_000_000`, and mints `1_000_000_000` ckUSDT to the user. [5](#0-4) 

6. Minter holds `999_900_000` USDT but has issued `1_000_000_000` ckUSDT — a `100_000` unit shortfall per deposit.
7. After many deposits, the minter's USDT balance is insufficient to honor all ckUSDT redemptions. The last withdrawers lose funds.

The root cause is entirely within the IC minter's `mint()` function: it unconditionally trusts `event.value()` from the Ethereum log without any on-chain balance verification, making it structurally incompatible with fee-on-transfer tokens regardless of which specific token triggers the condition. [6](#0-5) [3](#0-2)

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-102)
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
        {
            Ok(Ok(block_index)) => block_index.0.to_u64().expect("nat does not fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "Failed to mint {token_symbol}: {event:?} {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L37-48)
```text
|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]

|WBTC
|https://etherscan.io/token/0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599[0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599]

|wstETH
|https://etherscan.io/token/0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0[0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0]

|XAUt
|https://etherscan.io/token/0x68749665FF8D2d112Fa859AA293F07A622782F38[0x68749665FF8D2d112Fa859AA293F07A622782F38]
|===
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L191-210)
```rust
    fn record_event_to_mint(&mut self, event: &ReceivedEvent) {
        let event_source = event.source();
        assert!(
            !self.events_to_mint.contains_key(&event_source),
            "there must be no two different events with the same source"
        );
        assert!(!self.minted_events.contains_key(&event_source));
        assert!(!self.invalid_events.contains_key(&event_source));
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
        }

        self.events_to_mint.insert(event_source, event.clone());

        self.update_balance_upon_deposit(event)
    }
```
