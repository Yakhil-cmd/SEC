### Title
ckETH Minter Internal ERC-20 Accounting Cannot Capture Rebasing Token Yield — (`rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter tracks ERC-20 token balances using pure internal audit-event accounting (`erc20_balances`). This accounting is updated only on explicit deposit and withdrawal events. If a rebasing ERC-20 token (e.g., stETH, aUSDC) is added as a supported ckERC20 token via NNS proposal, the automatic balance increases from rebasing are never captured. The yield accumulates at the minter's Ethereum address but can never be claimed, minted as ckERC20, or returned to depositors.

---

### Finding Description

The `State` struct in the ckETH minter holds two internal balance trackers:

```rust
/// Current balance of ETH held by the minter.
/// Computed based on audit events.
pub eth_balance: EthBalance,

/// Current balance of ERC-20 tokens held by the minter.
/// Computed based on audit events.
pub erc20_balances: Erc20Balances,
``` [1](#0-0) 

The `EthBalance` struct's own documentation explicitly acknowledges the divergence:

> "Note that invalid deposits are not accounted for and so this value might be less than what is displayed by Etherscan or retrieved by the JSON-RPC call `eth_getBalance`. Also, some transactions may have gone directly to the minter's address without going via the helper smart contract." [2](#0-1) 

The public DID interface also documents this explicitly:

> "This might be less that the actual amount available on the `minter_address()`." [3](#0-2) 

The `Erc20Balances` struct is updated only via two paths:

1. `erc20_add` — called when a `ReceivedErc20Event` deposit is processed through the helper contract: [4](#0-3) 

2. `erc20_sub` — called when a finalized ERC-20 withdrawal transaction is confirmed: [5](#0-4) 

There is no code path that:
- Queries the actual on-chain ERC-20 balance of the minter's Ethereum address
- Detects that a supported token is rebasing
- Mints additional ckERC20 to reflect rebasing yield
- Provides any mechanism to claim or redistribute the accumulated rebasing yield

The `AddCkErc20Token` endpoint accepts any ERC-20 contract address with no check for rebasing behavior: [6](#0-5) 

The deposit flow mints ckERC20 strictly equal to `event.value` — the amount recorded in the `ReceivedEthOrErc20` log event at deposit time — never the current on-chain balance: [7](#0-6) 

---

### Impact Explanation

If a rebasing ERC-20 token (e.g., stETH, Aave aTokens, Blast USDB/WETH equivalents on Ethereum) is added as a supported ckERC20 token:

1. Users deposit `N` tokens → minter mints `N` ckERC20.
2. Over time, the rebasing mechanism increases the minter's actual on-chain ERC-20 balance to `N + yield`.
3. The minter's `erc20_balances` still records only `N`.
4. The `yield` portion is permanently stranded at the minter's Ethereum address.
5. No NNS proposal, no minter upgrade, and no user action can recover the yield without a code change, because the minter has no `eth_getBalance`-style reconciliation path for ERC-20 tokens.
6. The total ckERC20 supply on the IC ledger will exceed the minter's internal accounting but be less than the actual on-chain balance — creating a silent, growing discrepancy.

**Severity:** Loss of funds (yield permanently inaccessible). The magnitude grows continuously as long as the rebasing token remains supported.

---

### Likelihood Explanation

The ckETH minter is designed to support any ERC-20 token added via NNS proposal. The `AddCkErc20Token` message carries no rebasing flag or check. Popular, widely-trusted tokens such as stETH (Lido), aUSDC (Aave), or Blast's USDB/WETH are rebasing by design and are natural candidates for ckERC20 wrapping. The NNS governance process does not include automated token-property validation. A well-intentioned NNS proposal to add stETH as ckstETH would silently activate this yield-loss path for all depositors from that point forward.

---

### Recommendation

1. **Detect rebasing tokens before adding support.** Add a validation step in the `add_ckerc20_token` flow that checks whether the ERC-20 contract implements a rebasing interface (e.g., `rebase()`, `getRebaseIndex()`, or non-standard `balanceOf` behavior) and rejects such tokens.

2. **Implement a balance reconciliation mechanism.** Add a periodic task that calls `eth_call` with `balanceOf(minter_address)` for each supported ERC-20 token and compares the result to `erc20_balances`. If the on-chain balance exceeds the internal accounting, mint the difference as ckERC20 to a designated treasury or distribute it proportionally.

3. **Document the limitation explicitly.** Until a fix is deployed, document in the `AddCkErc20Token` proposal template that rebasing ERC-20 tokens are not supported and must not be added.

---

### Proof of Concept

1. NNS passes a proposal calling `add_ckerc20_token` with the stETH contract address (`0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84`).
2. User A deposits 100 stETH via the helper contract → minter's `erc20_balances` records `100 stETH`, mints 100 ckstETH.
3. Lido's daily rebase increases the minter's actual stETH balance to `100.05 stETH`.
4. `erc20_balances.balance_of(&steth_address)` still returns `100 stETH` — the `0.05 stETH` yield is invisible to the minter.
5. After one year at ~4% APY, the minter holds `~104 stETH` on-chain but its internal accounting shows `100 stETH`. The `~4 stETH` yield (~$15,000+ at current prices) is permanently inaccessible.
6. No endpoint on the minter canister can claim or redistribute this yield; `erc20_add` is only called from `update_balance_upon_deposit`, which is only triggered by `ReceivedEthOrErc20` log events from the helper contract. [8](#0-7) [4](#0-3)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L70-76)
```rust
    /// Current balance of ETH held by the minter.
    /// Computed based on audit events.
    pub eth_balance: EthBalance,

    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L729-770)
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L600-612)
```text
type AddCkErc20Token = record {
    // Ethereum chain ID.
    chain_id : nat;

    // The Ethereum address of the ERC-20 smart contract.
    address : text;

    // The ckERC20 token symbol on the ledger.
    ckerc20_token_symbol : text;

    // The ledger ID for that ckERC20 token.
    ckerc20_ledger_id : principal;
};
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
