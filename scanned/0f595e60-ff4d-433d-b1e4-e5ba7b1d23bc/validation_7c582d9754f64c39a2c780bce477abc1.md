### Title
ckETH Minter Mints Non-Withdrawable Dust Balances Due to Missing Minimum Deposit Check - (`File: rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter enforces a `minimum_withdrawal_amount` on the `withdraw_eth` endpoint but applies **no equivalent minimum on the deposit path**. Any ETH deposited via the Ethereum helper contract below `minimum_withdrawal_amount` is minted as ckETH that can never be converted back to ETH, permanently locking the underlying ETH in the minter's Ethereum address. This is the direct IC analog of the YToken missing-minimum-deposit-check vulnerability.

---

### Finding Description

The ckBTC minter correctly enforces **both** a deposit minimum (`deposit_btc_min_amount`) and a withdrawal minimum (`retrieve_btc_min_amount`). In `update_balance.rs`, UTXOs below `deposit_btc_min_amount` are silently ignored and never minted: [1](#0-0) 

The ckETH minter has **only** a withdrawal minimum. In `withdraw_eth()`, the check is: [2](#0-1) 

The deposit path in `mint()` processes every scraped `ReceivedEthEvent` or `ReceivedErc20Event` and calls `client.transfer()` with the raw `event.value()` — **no minimum amount check is performed**: [3](#0-2) 

The state validation in `validate_config()` only ensures `minimum_withdrawal_amount >= ledger_transfer_fee` (2,000,000,000,000 wei on mainnet), but imposes no floor on what can be deposited: [4](#0-3) 

The ckETH minter has no `deposit_minimum_amount` field at all, unlike the ckBTC minter which has `deposit_btc_min_amount`: [5](#0-4) 

The same gap applies to ckERC20 deposits, which are processed by the same `mint()` function: [6](#0-5) 

---

### Impact Explanation

An unprivileged user who calls the Ethereum helper contract `depositEth()` or `depositErc20()` with an amount below `minimum_withdrawal_amount` (currently 5,000,000,000,000,000 wei = 0.005 ETH) will:

1. Have their ETH transferred to the minter's Ethereum address (irreversible on-chain).
2. Receive ckETH minted to their IC account equal to the deposited amount.
3. Be **permanently unable** to call `withdraw_eth` because the amount is below `minimum_withdrawal_amount`.
4. If the deposited amount is also below the ckETH ledger transfer fee (2,000,000,000,000 wei), be **unable to even transfer** the ckETH to another account.

The ETH is permanently locked in the minter's Ethereum address with no recovery path. The minter has no admin mechanism to reimburse sub-minimum deposits (unlike the quarantine mechanism for failed mints). [7](#0-6) 

---

### Likelihood Explanation

**Medium.** The Ethereum helper contract `depositEth()` and `depositErc20()` accept any non-zero `msg.value` / `amount` with no minimum enforcement: [8](#0-7) 

A user unfamiliar with the IC-side minimum withdrawal amount (which is not surfaced in the Ethereum contract) can accidentally deposit a small amount. Additionally, a malicious actor can deliberately grief other users by sending dust ETH to their deposit addresses. The `minimum_withdrawal_amount` is a dynamic value that can be raised via governance upgrade, meaning a deposit that was previously withdrawable can become permanently locked after an upgrade.

---

### Recommendation

1. **Add a `deposit_minimum_amount` field** to the ckETH minter state (analogous to `deposit_btc_min_amount` in ckBTC), initialized to at least `minimum_withdrawal_amount`.
2. **Filter deposits below the minimum** in the log-scraping / event-processing path, recording them as `InvalidDeposit` events rather than minting ckETH for them.
3. **Validate the Ethereum helper contract** (or document clearly) that deposits below the IC-side minimum will result in permanently locked funds.
4. Consider enforcing the minimum in the `DepositHelperWithSubaccount.sol` contract itself to prevent the ETH from being transferred to the minter in the first place.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Attacker calls `depositEth(principal, subaccount)` on the Ethereum helper contract with `msg.value = 1 wei`.
2. The helper contract emits `ReceivedEthOrErc20(0x0, attacker, 1, principal, subaccount)` and transfers 1 wei to the minter's Ethereum address.
3. The ckETH minter's timer calls `scrape_logs()`, which picks up the event and adds it to `events_to_mint`.
4. The minter calls `mint()`, which calls `client.transfer(amount: 1)` on the ckETH ledger — minting 1 wei ckETH to the victim's IC account.
5. The victim calls `withdraw_eth(amount: 1)` → rejected: `amount (1) < minimum_withdrawal_amount (5_000_000_000_000_000)`.
6. The victim calls `icrc1_transfer(amount: 1)` → rejected: `amount (1) < transfer_fee (2_000_000_000_000)`.
7. The 1 wei ETH is permanently locked in the minter's Ethereum address. The 1 wei ckETH is permanently frozen in the victim's IC account. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L276-300)
```rust
    for utxo in processable_utxos {
        let ignored_reason = if utxo.value < deposit_btc_min_amount {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is lower than the minimum deposit amount {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(deposit_btc_min_amount)
            ))
        } else if utxo.value <= check_fee {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is not higher than the check fee {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(check_fee)
            ))
        } else {
            None
        };
        if let Some(ignored_reason) = ignored_reason {
            mutate_state(|s| {
                state::audit::ignore_utxo(s, utxo.clone(), caller_account, now, runtime)
            });
            log!(Priority::Debug, "{ignored_reason}");
            utxo_statuses.push(UtxoStatus::ValueTooSmall(utxo));
            continue;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L289-296)
```rust
    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L53-68)
```rust
        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens
                        .get_entry_alt(&event.erc20_contract_address)
                        .map(|(principal, symbol)| (symbol.to_string(), *principal))
                }) {
                    result
                } else {
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
            }
        };
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-101)
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L156-171)
```rust
        if self.cketh_minimum_withdrawal_amount == Wei::ZERO {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must be positive".to_string(),
            ));
        }
        let cketh_ledger_transfer_fee = match self.ethereum_network {
            EthereumNetwork::Mainnet => Wei::new(2_000_000_000_000),
            EthereumNetwork::Sepolia => Wei::new(10_000_000_000),
        };
        if self.cketh_minimum_withdrawal_amount < cketh_ledger_transfer_fee {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must cover ledger transaction fee, \
                otherwise ledger can return a BadBurn error that should be returned to the user"
                    .to_string(),
            ));
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/init.rs (L27-31)
```rust
    /// Minimum amount of bitcoin that can be deposited
    pub deposit_btc_min_amount: Option<u64>,

    /// Minimum amount of bitcoin that can be retrieved
    pub retrieve_btc_min_amount: u64,
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```
