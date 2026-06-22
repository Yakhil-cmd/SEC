### Title
Residual ETH Permanently Stranded at ckETH Minter's Ethereum Address Due to Incomplete Balance Tracking - (`rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `EthBalance` struct tracks only ETH that arrives through the designated helper smart contract. Any ETH sent directly to the minter's Ethereum address — bypassing the helper contract — is never credited to `eth_balance` and has no recovery path. The code itself acknowledges this gap in a comment, but provides no mechanism to harness or redistribute the residual ETH, making it permanently stranded.

---

### Finding Description

The `EthBalance` struct in `rs/ethereum/cketh/minter/src/state.rs` maintains a software-tracked ledger of ETH under the minter's control: [1](#0-0) 

The `eth_balance` field is updated exclusively through two event-driven paths:

- **Deposit**: `update_balance_upon_deposit` adds to `eth_balance` only when a `ReceivedEvent` is scraped from the helper contract logs.
- **Withdrawal**: `update_balance_upon_withdrawal` subtracts `debited_amount` (= `tx.amount + tx_fee`) from `eth_balance` after a finalized transaction receipt is observed. [2](#0-1) [3](#0-2) 

The log-scraping loop in `deposit.rs` only queries events emitted by the helper contract address: [4](#0-3) 

ETH that arrives at the minter's Ethereum address through any other channel — a plain ETH transfer, a self-destruct forwarding, or a contract interaction that does not emit a helper-contract event — is **never observed** by the scraper and therefore **never added** to `eth_balance`. The code comment explicitly acknowledges this: [5](#0-4) 

Because no ckETH is minted for such ETH, users cannot burn ckETH to withdraw it. Because there is no admin sweep endpoint, the minter cannot move it either. The ETH is permanently locked at the minter's tECDSA-controlled Ethereum address with no utilization path — a direct structural analog to the residual ETH in `SafEth.sol`.

The `total_unspent_tx_fees` accumulator (the difference between the fee ceiling charged to users and the actual gas consumed) is correctly retained inside `eth_balance` and is not stranded; the stranded funds are exclusively those arriving outside the helper-contract event flow. [6](#0-5) 

---

### Impact Explanation

Any ETH that reaches the minter's Ethereum address without a corresponding helper-contract log event is permanently inaccessible:

- No ckETH is minted for it, so no user can burn ckETH to reclaim it.
- No admin or governance endpoint exists to issue a raw ETH transfer from the minter's tECDSA key to recover it.
- The `eth_balance` metric reported on the dashboard and via Prometheus will permanently under-report the true on-chain balance, silently accumulating a growing discrepancy.
- Over time, the stranded ETH represents a real monetary loss to the ecosystem (ETH locked forever under a key that will never sign a recovery transaction). [7](#0-6) 

---

### Likelihood Explanation

The attack surface is fully open to any unprivileged actor:

1. **Direct ETH transfer**: Any Ethereum account can call `eth_sendTransaction` with `to = minter_address` and `value > 0`. No special permission is required.
2. **Self-destruct forwarding**: A malicious or buggy contract can `selfdestruct` and forward its ETH balance to the minter's address; such transfers bypass the helper-contract event entirely.
3. **Accidental sends**: Ordinary users who look up the minter's Ethereum address on Etherscan and send ETH directly (a common mistake) will permanently lose those funds.

None of these scenarios require any privileged role, governance majority, or threshold-key compromise.

---

### Recommendation

Add a governance-controlled or owner-controlled endpoint that issues a signed Ethereum transaction (via tECDSA) to sweep the surplus balance — i.e., `actual_eth_balance_on_chain - eth_balance` — to a designated recovery address or back into the helper contract. Alternatively, periodically reconcile `eth_balance` against the result of an `eth_getBalance` RPC call and credit any positive discrepancy as a recoverable surplus, minting ckETH to a treasury account or redistributing it proportionally to existing ckETH holders.

---

### Proof of Concept

1. Deploy the ckETH minter on a testnet. Observe `eth_balance = 0`.
2. Deposit 1 ETH through the helper contract. Observe `eth_balance = 1 ETH`.
3. From a separate EOA, send 0.5 ETH directly to the minter's Ethereum address (plain transfer, no helper contract involved).
4. Query `eth_getBalance(minter_address)` on-chain → returns `1.5 ETH`.
5. Query the minter's dashboard or Prometheus metric `cketh_minter_eth_balance` → returns `1 ETH`.
6. The 0.5 ETH discrepancy has no code path to be recovered: no ckETH was minted for it, no sweep function exists, and the scraper will never emit an event for a plain ETH transfer. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-339)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L341-384)
```rust
    fn update_balance_upon_withdrawal(
        &mut self,
        withdrawal_id: &LedgerBurnIndex,
        receipt: &TransactionReceipt,
    ) {
        let tx_fee = receipt.effective_transaction_fee();
        let tx = self
            .eth_transactions
            .get_finalized_transaction(withdrawal_id)
            .expect("BUG: missing finalized transaction");
        let withdrawal_request = self
            .eth_transactions
            .get_processed_withdrawal_request(withdrawal_id)
            .expect("BUG: missing withdrawal request");
        let charged_tx_fee = match withdrawal_request {
            WithdrawalRequest::CkEth(req) => req
                .withdrawal_amount
                .checked_sub(tx.transaction().amount)
                .expect("BUG: withdrawal amount MUST always be at least the transaction amount"),
            WithdrawalRequest::CkErc20(req) => req.max_transaction_fee,
        };
        let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
            "BUG: charged transaction fee MUST always be at least the effective transaction fee",
        );
        let debited_amount = match receipt.status {
            TransactionStatus::Success => tx
                .transaction()
                .amount
                .checked_add(tx_fee)
                .expect("BUG: debited amount always fits into U256"),
            TransactionStatus::Failure => tx_fee,
        };
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);

        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L647-661)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct EthBalance {
    /// Amount of ETH controlled by the minter's address via tECDSA.
    /// Note that invalid deposits are not accounted for and so this value
    /// might be less than what is displayed by Etherscan
    /// or retrieved by the JSON-RPC call `eth_getBalance`.
    /// Also, some transactions may have gone directly to the minter's address
    /// without going via the helper smart contract.
    eth_balance: Wei,
    /// Total amount of fees across all finalized transactions ckETH -> ETH.
    total_effective_tx_fees: Wei,
    /// Total amount of fees that were charged to the user during the withdrawal
    /// but not consumed by the finalized transaction ckETH -> ETH
    total_unspent_tx_fees: Wei,
}
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L185-233)
```rust
async fn scrape_until_block<S>(last_block_number: BlockNumber, max_block_spread: u16)
where
    S: LogScraping,
{
    let scrape = match read_state(S::next_scrape) {
        Some(s) => s,
        None => {
            log!(
                DEBUG,
                "[scrape_contract_logs]: skipping scraping {} logs: not active",
                S::ID
            );
            return;
        }
    };
    let block_range = BlockRangeInclusive::new(
        scrape
            .last_scraped_block_number
            .checked_increment()
            .unwrap_or(BlockNumber::MAX),
        last_block_number,
    );
    log!(
        DEBUG,
        "[scrape_contract_logs]: Scraping {} logs in block range {block_range}",
        S::ID
    );
    let rpc_client = read_state(rpc_client);
    for block_range in block_range.into_chunks(max_block_spread) {
        match scrape_block_range::<S>(
            &rpc_client,
            scrape.contract_address,
            scrape.topics.clone(),
            block_range.clone(),
        )
        .await
        {
            Ok(()) => {}
            Err(e) => {
                log!(
                    INFO,
                    "[scrape_contract_logs]: Failed to scrape {} logs in range {block_range}: {e:?}",
                    S::ID
                );
                return;
            }
        }
    }
}
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L311-360)
```rust
pub fn register_deposit_events(
    scraping_id: LogScrapingId,
    transaction_events: Vec<ReceivedEvent>,
    errors: Vec<ReceivedEventError>,
) {
    for event in transaction_events {
        log!(
            INFO,
            "Received event {event:?}; will mint {} {scraping_id} to {}",
            event.value(),
            event.beneficiary()
        );
        if crate::blocklist::is_blocked(&event.from_address()) {
            log!(
                INFO,
                "Received event from a blocked address: {} for {} {scraping_id}",
                event.from_address(),
                event.value(),
            );
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: event.source(),
                        reason: format!("blocked address {}", event.from_address()),
                    },
                )
            });
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
        }
    }
    if read_state(State::has_events_to_mint) {
        ic_cdk_timers::set_timer(Duration::from_secs(0), async { mint().await });
    }
    for error in errors {
        if let ReceivedEventError::InvalidEventSource { source, error } = &error {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: *source,
                        reason: error.to_string(),
                    },
                )
            });
        }
        report_transaction_error(error);
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L973-995)
```rust
                w.encode_gauge(
                    "cketh_minter_eth_balance",
                    s.eth_balance.eth_balance().as_f64(),
                    "Known amount of ETH on the minter's address",
                )?;
                let mut erc20_balances = w.gauge_vec(
                    "cketh_minter_erc20_balances",
                    "Known amount of ERC-20 on the minter's address",
                )?;
                for (token, balance) in s.erc20_balances_by_token_symbol().iter() {
                    erc20_balances = erc20_balances
                        .value(&[("erc20_token", &token.to_string())], balance.as_f64())?;
                }
                w.encode_gauge(
                    "cketh_minter_total_effective_tx_fees",
                    s.eth_balance.total_effective_tx_fees().as_f64(),
                    "Total amount of fees across all finalized transactions ckETH -> ETH",
                )?;
                w.encode_gauge(
                    "cketh_minter_total_unspent_tx_fees",
                    s.eth_balance.total_unspent_tx_fees().as_f64(),
                    "Total amount of unspent fees across all finalized transaction ckETH -> ETH",
                )?;
```
