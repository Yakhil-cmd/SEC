### Title
Rebasing ERC-20 Token Deposits Permanently Lock Excess Funds in ckETH Minter — (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary
The ckETH minter tracks ERC-20 balances using a purely event-driven accounting model (`Erc20Balances`). If a rebasing ERC-20 token (e.g., stETH, aUSDC, aDAI) is added as a supported ckERC20 token via governance, the rebasing yield that accrues at the minter's Ethereum address is never observed, never credited, and permanently locked — with no on-chain recovery path.

### Finding Description

The `Erc20Balances` struct in `rs/ethereum/cketh/minter/src/state.rs` maintains a `BTreeMap<Address, Erc20Value>` that is updated exclusively through two paths:

- `erc20_add` — called from `update_balance_upon_deposit` when a `ReceivedErc20` log event is scraped from Ethereum.
- `erc20_sub` — called from `update_balance_upon_withdrawal` when a finalized withdrawal receipt is processed. [1](#0-0) [2](#0-1) 

The minting path in `rs/ethereum/cketh/minter/src/deposit.rs` mints exactly `event.value()` — the amount recorded in the Ethereum log — and nothing more. [3](#0-2) 

The `MinterInfo` DID and the `EthBalance` struct documentation both explicitly acknowledge the divergence:

> "This might be less that the actual amount available on the `minter_address()`." [4](#0-3) [5](#0-4) 

For non-rebasing tokens this is benign — the balance only changes through deposits and withdrawals. For rebasing tokens, the actual ERC-20 balance at the minter's Ethereum address grows continuously between events. The minter's internal ledger never observes this growth, so the surplus is silently stranded. There is no `withdraw_excess_erc20` endpoint, no sweep function, and no governance-callable recovery path anywhere in the minter. [6](#0-5) 

### Impact Explanation

If a rebasing ERC-20 token (e.g., Lido stETH, Aave aUSDC) is added as a supported ckERC20 token:

1. User deposits 1 000 stETH → minter mints 1 000 ckstETH (tracked: 1 000).
2. stETH rebases over time → minter's Ethereum address holds 1 050 stETH.
3. Minter's `erc20_balances` still records 1 000 stETH.
4. User burns 1 000 ckstETH → receives 1 000 stETH.
5. The 50 stETH rebasing surplus is permanently locked at the minter's tECDSA-controlled Ethereum address.
6. `erc20_sub` would panic on any attempt to withdraw more than the tracked amount, making the surplus irrecoverable even by the minter itself. [7](#0-6) 

The locked surplus accrues to no one — it cannot be swept to the SNS treasury, refunded to depositors, or recovered via any existing endpoint.

### Likelihood Explanation

The trigger requires NNS/SNS governance to add a rebasing ERC-20 token as a supported ckERC20 token. This can happen in good faith: stETH and Aave aTokens are among the most liquid ERC-20 tokens on Ethereum, and a governance proposal to support them is plausible. The minter code contains no type-level guard, no documentation warning, and no runtime check that would prevent a rebasing token from being registered. Once registered, every ordinary user deposit through the helper contract activates the loss path — no privileged action is needed after that point. [8](#0-7) 

### Recommendation

1. **Document the restriction** in the `add_ckerc20_token` governance path: rebasing tokens must not be added as supported ckERC20 tokens.
2. **Add a sweep/recovery endpoint** callable by NNS governance that computes `actual_on_chain_balance − tracked_balance` for a given ERC-20 contract and issues an Ethereum transaction to transfer the surplus to a designated treasury address.
3. **Alternatively**, replace event-value-based minting with a balance-diff approach: query `eth_getBalance` / `balanceOf` before and after each deposit transaction and mint the observed difference, so rebasing yield is captured rather than silently lost.

### Proof of Concept

**Entry path** (no malicious actor required):

1. NNS governance proposal: `add_ckerc20_token` with `erc20_contract_address = stETH_mainnet`.
2. User calls `depositEth` on the ERC-20 helper contract, depositing 1 000 stETH.
3. Minter scrapes the `ReceivedErc20` log; `update_balance_upon_deposit` calls `erc20_add(stETH, 1_000e18)`.
4. Minter mints 1 000 ckstETH to the user.
5. 30 days pass; stETH rebases at ~4 % APY → minter address holds ≈ 1 003.3 stETH on-chain.
6. `erc20_balances.balance_of(stETH)` still returns `1_000e18`.
7. User burns 1 000 ckstETH; minter sends 1 000 stETH; `erc20_sub(stETH, 1_000e18)` succeeds.
8. `erc20_balances.balance_of(stETH)` → `0`; actual on-chain balance → `3.3e18` stETH.
9. No endpoint exists to recover the 3.3 stETH; it is permanently locked. [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L98-103)
```rust
    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
    /// - value: ckERC20 token symbol
    pub ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>,
}
```

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L377-383)
```rust
        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L648-661)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L729-771)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct Erc20Balances {
    balance_by_erc20_contract: BTreeMap<Address, Erc20Value>,
}

impl Erc20Balances {
    pub fn balance_of(&self, erc20_contract: &Address) -> Erc20Value {
        *self
            .balance_by_erc20_contract
            .get(erc20_contract)
            .unwrap_or(&Erc20Value::ZERO)
    }

    pub fn erc20_add(&mut self, erc20_contract: Address, deposit: Erc20Value) {
        match self.balance_by_erc20_contract.get(&erc20_contract) {
            Some(previous_value) => {
                let new_value = previous_value.checked_add(deposit).unwrap_or_else(|| {
                    panic!("BUG: overflow when adding {deposit} to {previous_value}")
                });
                self.balance_by_erc20_contract
                    .insert(erc20_contract, new_value);
            }
            None => {
                self.balance_by_erc20_contract
                    .insert(erc20_contract, deposit);
            }
        }
    }

    pub fn erc20_sub(&mut self, erc20_contract: Address, withdrawal_amount: Erc20Value) {
        let previous_value = self
            .balance_by_erc20_contract
            .get(&erc20_contract)
            .expect("BUG: Cannot subtract from a missing ERC-20 balance");
        let new_value = previous_value
            .checked_sub(withdrawal_amount)
            .unwrap_or_else(|| {
                panic!("BUG: underflow when subtracting {withdrawal_amount} from {previous_value}")
            });
        self.balance_by_erc20_contract
            .insert(erc20_contract, new_value);
    }
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L217-226)
```text
    // Amount of ETH in Wei controlled by the minter.
    // This might be less that the actual amount available on the `minter_address()`.
    eth_balance : opt nat;

    // Last gas fee estimate.
    last_gas_fee_estimate: opt GasFeeEstimate;

    // Amount of ETH in Wei controlled by the minter.
    // This might be less that the actual amount available on the `minter_address()`.
    erc20_balances : opt vec record { erc20_contract_address: text; balance: nat};
```
